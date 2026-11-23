"""Persist scoped processing jobs and publish a complete generation in one transaction."""

import json
import time
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import and_, insert, or_, select, update

from dovideo.application.edit_timeline import timestamp
from dovideo.application.media_sampling import identity, PIPELINE
from .edit_repository import EditRepository
from .edit_tables import jobs as render_jobs, revisions
from .processing_tables import records, runs
from .source_tables import projects, sources
from .work_outbox import emit


def error(code, message, retryable=False):
    return dict(code=code, message=message, retryable=retryable, request_id=str(uuid4()), details={})


def view(row):
    return dict(
        processing_run_id=row.id,
        workspace_id=row.workspace,
        media_revision_id=row.revision,
        status=row.status,
        stage=row.stage,
        completed_units=row.completed,
        total_units=row.total,
        pipeline_version=row.config.get("pipeline_version", PIPELINE),
        index_generation_id=(
            row.config.get("_generation") if row.status in ("SUCCEEDED", "PARTIAL") else None
        ),
        error=row.error,
        cancel_requested_at=row.cancelled,
    )


def record(run, kind, unit, body, start_us=0, *, rid=None):
    return dict(
        id=rid or identity(run.id, f"{kind}:{unit}"),
        workspace=run.workspace,
        asset=run.asset,
        run=run.id,
        kind=kind,
        unit=str(unit),
        body=body,
        start_us=start_us,
    )


def source_query(scope, asset):
    return select(sources).where(sources.c.workspace == scope, sources.c.id == asset)


class StaleAttempt(Exception):
    pass


class MediaCatalog:
    def __init__(self, sources_store):
        self.sources = sources_store
        self.sessions = sources_store.sessions
        self.write = EditRepository(sources_store).write
        self.root = sources_store.root / "processing"

    @staticmethod
    def register(db, source, config, *, kind="INDEX"):
        key = f"{kind}:{config['idempotency_key']}"
        rid = identity(source.id, key)
        row = db.execute(select(runs).where(runs.c.id == rid)).first()
        if row:
            if {k: v for k, v in row.config.items() if not k.startswith("_")} != {
                k: v for k, v in config.items() if not k.startswith("_")
            }:
                raise HTTPException(409, "幂等键已用于另一处理配置")
            return view(row)
        probe = source.probe or {}
        if probe.get("deleted"):
            raise HTTPException(404, "素材已删除")
        db.execute(
            insert(runs).values(
                id=rid,
                workspace=source.workspace,
                asset=source.id,
                revision=source.revision,
                project=source.project,
                predecessor=probe.get("active_run"),
                kind=kind,
                config=config,
                stage="QUEUED",
                status="QUEUED",
                attempt=0,
                lease_end=0,
                completed=0,
                total=None,
                created=timestamp(),
            )
        )
        if kind == "INDEX" and not probe.get("active_run"):
            pending = [
                dict(name=name, state="PENDING", reason=None) for name in config["requested_capabilities"]
            ]
            db.execute(
                sources.update()
                .where(sources.c.id == source.id)
                .values(probe=dict(probe, capabilities=pending))
            )
        return view(db.execute(select(runs).where(runs.c.id == rid)).one())

    def enqueue(self, scope, asset, config):
        with self.write() as db:
            source = db.execute(source_query(scope, asset).with_for_update()).first()
            if not source or not source.receipt or (source.probe or {}).get("deleted"):
                raise HTTPException(404, "素材不可用")
            return self.register(db, source, config)

    @staticmethod
    def finished(db, row):
        emit(db, identity(row.id, "finished"), row.workspace, row.id, "processing.finished")

    def get(self, scope, rid):
        with self.sessions() as db:
            row = db.execute(select(runs).where(runs.c.workspace == scope, runs.c.id == rid)).first()
        if row is None:
            raise HTTPException(404, "处理任务不存在")
        return view(row)

    def cancel(self, scope, rid):
        with self.write() as db:
            row = db.execute(
                select(runs).where(runs.c.id == rid, runs.c.workspace == scope).with_for_update()
            ).first()
            if not row:
                raise HTTPException(404, "处理任务不存在")
            if row.kind == "DELETE":
                raise HTTPException(409, "素材删除已提交")
            if row.status in ("RUNNING", "QUEUED"):
                db.execute(
                    runs.update()
                    .where(runs.c.id == rid)
                    .values(status="CANCELLED", stage="CANCELLED", cancelled=timestamp(), lease_end=0)
                )
                self.finished(db, row)
        return self.get(scope, rid)

    def visual_usage(self, scope, rid):
        self.get(scope, rid)
        with self.sessions() as db:
            history = list(
                db.scalars(
                    select(records.c.body).where(
                        records.c.workspace == scope,
                        records.c.run == rid,
                        records.c.kind == "VISUAL_CHARGE",
                    )
                )
            )
        return dict(
            currency="CNY",
            model_calls=len(history),
            images=sum(r["images"] for r in history),
            committed_microunits=sum(r["charged"] or 0 for r in history),
            uncertain_microunits=sum(r["reserved"] for r in history if r["charged"] is None),
            attempts=history,
        )

    def claim(self):
        now = int(time.time())
        available = or_(runs.c.status == "QUEUED", and_(runs.c.status == "RUNNING", runs.c.lease_end < now))
        with self.write() as db:
            row = db.execute(
                select(runs).where(available).order_by(runs.c.created).limit(1).with_for_update()
            ).first()
            if row is None:
                return None
            db.execute(
                update(runs)
                .where(runs.c.id == row.id)
                .values(status="RUNNING", attempt=row.attempt + 1, lease_end=now + 90)
            )
            return db.execute(select(runs).where(runs.c.id == row.id)).one()

    @staticmethod
    def current(run):
        return and_(runs.c.id == run.id, runs.c.attempt == run.attempt, runs.c.status == "RUNNING")

    def heartbeat(self, run):
        with self.write() as db:
            changed = db.execute(
                update(runs).where(self.current(run)).values(lease_end=int(time.time()) + 90)
            )
            return changed.rowcount == 1

    def commit(self, run, items=(), **state):
        with self.write() as db:
            changed = db.execute(
                update(runs).where(self.current(run)).values(lease_end=int(time.time()) + 90, **state)
            )
            if not changed.rowcount:
                raise StaleAttempt()
            if state.get("status") in ("SUCCEEDED", "FAILED", "PARTIAL", "CANCELLED"):
                self.finished(db, run)
            if items:
                ids = {item["id"]: item for item in items}
                seen = set(db.execute(select(records.c.id).where(records.c.id.in_(ids))).scalars())
                fresh = [v for k, v in ids.items() if k not in seen]
                if fresh:
                    db.execute(insert(records), fresh)

    def entries(self, run, kind):
        with self.sessions() as db:
            return db.execute(
                select(records)
                .where(records.c.run == run.id, records.c.kind == kind)
                .order_by(records.c.start_us, records.c.id)
            ).all()

    def publish(self, run, *, capabilities, proxy, problems, total):
        with self.write() as db:
            db.execute(select(projects.c.id).where(projects.c.id == run.project).with_for_update()).one()
            source = db.execute(source_query(run.workspace, run.asset).with_for_update()).one()
            if (source.probe or {}).get("deleted"):
                raise StaleAttempt()
            current = db.execute(select(runs).where(self.current(run)).with_for_update()).first()
            if current is None:
                raise StaleAttempt()
            self.finished(db, run)
            probe = dict(source.probe)
            if probe.get("active_run") != run.predecessor:
                db.execute(
                    update(runs)
                    .where(self.current(run))
                    .values(
                        status="FAILED",
                        stage="PUBLISH",
                        error=error("GENERATION_CONFLICT", "另一个版本已发布，请重新创建处理任务", True),
                    )
                )
                return
            probe.update(capabilities=capabilities, active_run=run.id, **proxy)
            db.execute(update(sources).where(sources.c.id == source.id).values(probe=probe))
            _, heads, generation = self.snapshot(db, run.workspace, run.project)
            manifest = record(
                run, "GENERATION", "manifest", {"project": run.project, "heads": heads}, rid=generation
            )
            db.execute(insert(records).values(**manifest))
            db.execute(
                update(runs)
                .where(self.current(run))
                .values(
                    status="PARTIAL" if problems else "SUCCEEDED",
                    stage="PUBLISHED",
                    completed=total,
                    total=total,
                    lease_end=0,
                    config=dict(run.config, _generation=generation),
                    error=(
                        error("PARTIAL_CAPABILITIES", "；".join(dict.fromkeys(problems)), True)
                        if problems
                        else None
                    ),
                )
            )

    def page(self, scope, asset, kind, cursor, limit):
        self.sources.asset(scope, asset)
        table = runs if kind == "RUN" else records
        req = select(table).where(table.c.workspace == scope, table.c.asset == asset)
        if table is records:
            with self.sessions() as db:
                source = db.execute(source_query(scope, asset)).one()
                active = source.probe.get("active_run") if source.probe else None
                latest = db.execute(
                    select(runs.c.id)
                    .where(runs.c.asset == asset, runs.c.kind == "INDEX")
                    .order_by(runs.c.created.desc())
                    .limit(1)
                ).scalar()
            req = req.where(
                records.c.run == (latest if kind == "COVERAGE" else active), records.c.kind == kind
            )
        with self.sessions() as db:
            history = table is runs
            sort = table.c.created if history else table.c.start_us
            if cursor:
                prior = db.execute(req.where(table.c.id == cursor)).first()
                if prior is None:
                    raise HTTPException(422, "分页位置不属于当前列表，请刷新")
                value = prior.created if history else prior.start_us
                req = req.where(
                    or_(sort < value, and_(sort == value, table.c.id < cursor))
                    if history
                    else or_(sort > value, and_(sort == value, table.c.id > cursor))
                )
            ordering = (sort.desc(), table.c.id.desc()) if history else (sort, table.c.id)
            rows = db.execute(req.order_by(*ordering).limit(limit + 1)).all()
        return dict(
            items=[view(r) if table is runs else r.body for r in rows[:limit]],
            next_cursor=rows[limit - 1].id if len(rows) > limit else None,
        )

    def get_record(self, scope, rid, kind):
        with self.sessions() as db:
            row = db.execute(
                select(records).where(
                    records.c.workspace == scope, records.c.id == rid, records.c.kind == kind
                )
            ).first()
        if row is not None:
            self.sources.asset(scope, row.asset)
        return row

    def evidence(self, scope, rid):
        row = self.get_record(scope, rid, "EVIDENCE")
        if not row:
            raise HTTPException(404, "证据不存在")
        run = self.get(scope, row.run)
        return dict(row.body["evidence"], index_generation_id=run["index_generation_id"])

    def snapshot(self, db, scope, project, pinned=None):
        assets = db.execute(
            select(sources.c.revision, sources.c.probe, sources.c.receipt).where(
                sources.c.workspace == scope, sources.c.project == project
            )
        )
        live = {a.revision: a for a in assets if a.receipt and not (a.probe or {}).get("deleted")}
        heads = {k: a.probe["active_run"] for k, a in live.items() if a.probe.get("active_run")}
        generation = identity(project, json.dumps(sorted(heads.items())))
        if pinned is not None and pinned != generation:
            manifest = db.execute(
                select(records.c.body).where(
                    records.c.id == pinned, records.c.workspace == scope, records.c.kind == "GENERATION"
                )
            ).scalar_one_or_none()
            if not manifest or manifest["project"] != project:
                raise HTTPException(404, "索引版本不存在")
            heads = {k: v for k, v in manifest["heads"].items() if k in live}
            generation = pinned
        return live, heads, generation

    def search_rows(self, rows, generation):
        result = []
        for item in rows:
            evidence = dict(item["evidence"], index_generation_id=generation)
            result.append(
                dict(
                    evidence,
                    source_generation_id=item["evidence"]["index_generation_id"],
                    modality=item["modality"],
                    source=item["source"],
                    frame=item.get("frame"),
                    path=(
                        self.derived_path(evidence["workspace_id"], item["file"])
                        if item.get("file")
                        else None
                    ),
                    segment_id=item["segment_id"],
                )
            )
        return result

    def derived_path(self, workspace, relative):
        path = self.root / relative
        if self.sources.objects and not path.is_file():
            self.sources.objects.fetch(f"videomind/processing/{workspace}/{relative}", path.stem, path)
        return path

    def search_scope(self, scope, project, media_revision_ids=None, *, generation_id=None):
        self.sources.project(scope, project)
        requested = set(media_revision_ids or [])
        with self.sessions() as db:
            live, heads, generation = self.snapshot(db, scope, project, generation_id)
            if requested - live.keys():
                raise HTTPException(404, "素材版本不可访问")
            selected = [v for k, v in heads.items() if not requested or k in requested]
            spaces = dict(
                db.execute(
                    select(records.c.run, records.c.body).where(
                        records.c.workspace == scope,
                        records.c.run.in_(selected),
                        records.c.kind == "CHECKPOINT",
                        records.c.unit == "search-spaces",
                    )
                ).all()
            )
            rows = db.execute(
                select(records.c.body).where(
                    records.c.workspace == scope, records.c.run.in_(selected), records.c.kind == "EVIDENCE"
                )
            ).scalars()
            result = self.search_rows(rows, generation)
            for row in result:
                row["search_spaces"] = spaces.get(row["source_generation_id"], {})
            return generation, result

    def literal_coverage(self, scope, project, generation_id, revision_ids):
        self.sources.project(scope, project)
        with self.sessions() as db:
            active, heads, _ = self.snapshot(db, scope, project, generation_id)
            covered = {}
            for rid in revision_ids:
                if rid not in heads:
                    continue
                row = active[rid]
                sub = db.execute(
                    select(records.c.body).where(
                        records.c.run == heads[rid],
                        records.c.kind == "CHECKPOINT",
                        records.c.unit == "subtitles",
                    )
                ).scalar_one_or_none()
                if sub and sub.get("complete"):
                    covered[rid] = [dict(start_us=0, end_us=row.probe["duration_us"])]
            return covered

    def delete_asset(self, scope, asset):
        with self.write() as db:
            source = db.execute(source_query(scope, asset).with_for_update()).first()
            if not source:
                raise HTTPException(404, "素材不存在")
            job = self.register(db, source, {"idempotency_key": "delete-asset"}, kind="DELETE")
            if (source.probe or {}).get("deleted"):
                return job
            probe = dict(source.probe or {}, deleted=True)
            probe.setdefault("deleted_at", int(time.time()))
            db.execute(update(sources).where(sources.c.id == asset).values(probe=probe))
            pending = db.execute(
                select(runs)
                .where(
                    runs.c.asset == asset, runs.c.kind == "INDEX", runs.c.status.in_(("QUEUED", "RUNNING"))
                )
                .with_for_update()
            ).all()
            for row in pending:
                db.execute(
                    update(runs)
                    .where(runs.c.id == row.id)
                    .values(status="CANCELLED", stage="CANCELLED", lease_end=0, cancelled=timestamp())
                )
                self.finished(db, row)
            pending_renders = db.execute(
                select(render_jobs, revisions.c.snapshot)
                .join(revisions, revisions.c.id == render_jobs.c.revision_id)
                .where(
                    render_jobs.c.workspace_id == scope,
                    render_jobs.c.status.in_(("QUEUED", "RUNNING", "VALIDATING")),
                )
                .with_for_update()
            ).all()
            for row in pending_renders:
                if any(clip["media_revision_id"] == source.revision for clip in row.snapshot["clips"]):
                    db.execute(
                        update(render_jobs)
                        .where(render_jobs.c.id == row.id)
                        .values(
                            status="CANCELLED",
                            cache_slot=None,
                            attempt=row.attempt + 1,
                            lease_until=0,
                            result=dict(row.result, cancel_requested_at=timestamp()),
                        )
                    )
                    emit(db, str(uuid4()), scope, row.id, "render.finished")
        return job
