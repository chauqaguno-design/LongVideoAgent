"""Timeline transactions. An edit, its receipt and its event commit together."""
import hashlib
import json
import os
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path

from sqlalchemy import and_, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from cutsieve.application.edit_timeline import EditError, apply_patch, empty_revision, invalid, missing, timestamp, uid
from cutsieve.application.render_formats import EXPORT_FORMATS
from cutsieve.infrastructure.edit_media import RENDERER_VERSION, output_frame
from .edit_tables import events, jobs, projects, receipts, revisions
from .work_outbox import emit


def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def caption_font(revision):
    if revision["caption_policy"] != "burn_aligned":
        return None
    font = Path(os.getenv("CUTSIEVE_CAPTION_FONT", ""))
    if not font.is_file():
        raise EditError(503, "CAPABILITY_UNAVAILABLE", "请配置 CUTSIEVE_CAPTION_FONT 指向用于烧录字幕的字体文件")
    return font


def render_key(workspace, revision, profile, font, export_format="mp4"):
    fields = [workspace, revision["revision_id"], profile, RENDERER_VERSION]
    if export_format != "mp4":
        fields.append(export_format)
    if font:
        with font.open("rb") as stream:
            fields.append(hashlib.file_digest(stream, "sha256").hexdigest())
    return digest(fields)


def project_view(r):
    return dict(edit_project_id=r.id, workspace_id=r.workspace_id, project_id=r.project_id,
                title=r.title, head_revision=r.head, head_revision_id=r.head_revision_id)


def job_view(r):
    result = {"renderer_version": "cutsieve-ffmpeg-v1", **r.result}
    result.pop("export_format", None)
    return dict(render_job_id=r.id, workspace_id=r.workspace_id, revision_id=r.revision_id,
                profile=r.profile, status=r.status, cache_key=r.cache_key, **result)


def owned(table, workspace, key):
    return select(table).where(table.c.workspace_id == workspace, table.c.id == key)


def one(db, query):
    row = db.execute(query).first()
    if row is None:
        raise missing()
    return row


class EditRepository:
    def __init__(self, sources):
        self.sources = sources
        self.sessions = sources.sessions

    @contextmanager
    def write(self):
        with self.sessions.begin() as db:
            # SQLite lacks SELECT FOR UPDATE. Take the writer slot before reading a head.
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            yield db

    def with_receipt(self, workspace, scope, kind, body, run, guard=None):
        # Preserve case-sensitive key identity under MySQL's default collation.
        key = digest(body["idempotency_key"])
        signature = digest(body)
        predicate = and_(receipts.c.workspace_id == workspace, receipts.c.scope_id == scope,
                         receipts.c.kind == kind, receipts.c.key == key)
        # Two transactions can attempt the first insert of the same key/cache slot.
        # Roll back the losing transaction and read the winner using a fresh snapshot.
        for attempt in range(2):
            try:
                with self.write() as db:
                    if guard is not None:
                        guard(db)
                    previous = db.execute(select(receipts).where(predicate)).first()
                    if previous:
                        if previous.digest != signature:
                            raise EditError(409, "IDEMPOTENCY_CONFLICT", "该幂等键已用于其他请求")
                        return previous.result, True
                    # Reserve the key before checking expected_revision.
                    db.execute(insert(receipts).values(workspace_id=workspace, scope_id=scope,
                               kind=kind, key=key, digest=signature, result={}))
                    answer = run(db)
                    db.execute(update(receipts).where(predicate).values(result=answer))
                    return answer, False
            except IntegrityError:
                if attempt:
                    raise

    def create(self, workspace, body, *, guard=None):
        self.sources.project(workspace, body["project_id"])
        def save(db):
            edit_id = uid()
            rev = empty_revision(workspace, edit_id, body["output_profile"])
            db.execute(insert(projects).values(id=edit_id, workspace_id=workspace,
                       project_id=body["project_id"], title=body["title"], head=0,
                       head_revision_id=rev["revision_id"]))
            self.insert_revision(db, rev)
            return project_view(one(db, owned(projects, workspace, edit_id)))
        return self.with_receipt(workspace, body["project_id"], "create", body, save, guard)[0]

    @staticmethod
    def insert_revision(db, rev):
        db.execute(insert(revisions).values(id=rev["revision_id"], workspace_id=rev["workspace_id"],
                   edit_id=rev["edit_project_id"], number=rev["revision_no"], snapshot=rev))

    def get(self, table, workspace, key, view, **filters):
        query = owned(table, workspace, key).filter_by(**filters)
        with self.sessions() as db:
            return view(one(db, query))

    def page(self, table, workspace, view, filters, cursor, limit):
        query = select(table).where(table.c.workspace_id == workspace).filter_by(**filters)
        # Revisions have a monotonic number; descending order puts head first.
        sort_key = table.c.number if table is revisions else table.c.id
        if cursor is not None:
            if table is revisions:
                if not cursor.isascii() or not cursor.isdigit() or len(cursor) > 16:
                    raise invalid("无效的版本分页游标")
                cursor = int(cursor)
            query = query.where(sort_key < cursor)
        with self.sessions() as db:
            rows = db.execute(query.order_by(sort_key.desc()).limit(limit + 1)).all()
            return dict(items=[view(r) for r in rows[:limit]],
                        next_cursor=str(rows[limit-1]._mapping[sort_key]) if len(rows) > limit else None)

    def patch(self, workspace, edit_id, body, *, guard=None, brief_id=None, validate_result=None):
        def save(db):
            parent = one(db, owned(projects, workspace, edit_id).with_for_update())
            if parent.head != body["expected_revision"]:
                raise EditError(409, "REVISION_CONFLICT", "时间线已更新，请重新读取当前版本")
            def load_rev(key):
                return one(db, owned(revisions, workspace, key).where(revisions.c.edit_id == edit_id).with_for_update()).snapshot
            checked = {}
            def check_clip(c):
                mid = c["media_revision_id"]
                if mid not in checked:
                    media = self.sources.media(workspace, mid)
                    asset = self.sources.asset(workspace, media["asset_id"])
                    if asset["project_id"] != parent.project_id:
                        raise missing()
                    checked[mid] = media
                if not 0 <= c["source_start_us"] < c["source_end_us"] <= checked[mid]["duration_us"]:
                    raise invalid("片段时间超出素材范围")
                if track := c.get("caption_track_id"):
                    self.resolve_track(workspace, mid, track)
                for key in c["evidence_ids"]:
                    resolve = getattr(self, "resolve_evidence", None)
                    if resolve is None:
                        raise EditError(503, "CAPABILITY_UNAVAILABLE", "证据索引暂不可用")
                    evidence = resolve(workspace, key, session=db)
                    if evidence["project_id"] != parent.project_id or evidence["media_revision_id"] != mid:
                        raise missing()
                    interval = evidence["range"]
                    if interval["end_us"] <= c["source_start_us"] or interval["start_us"] >= c["source_end_us"]:
                        raise invalid("证据的时间与片段不重合")
                if "crop" in c:
                    crop = c["crop"]
                    width, height = self.sources.display_size(workspace, mid)
                    if crop["x"] + crop["width"] > width or crop["y"] + crop["height"] > height:
                        raise invalid("裁剪框超出显示画面")

            before = load_rev(parent.head_revision_id)
            rev, assignments = apply_patch(before, body["operations"], load_rev, check_clip)
            if validate_result is not None:
                validate_result(before, rev)
            self.sources.require_readable(db, workspace, checked)
            if brief_id is not None:
                rev["source_brief_id"] = brief_id
            updated = db.execute(update(projects).where(projects.c.id == edit_id, projects.c.head == parent.head)
                                 .values(head=rev["revision_no"], head_revision_id=rev["revision_id"]))
            if updated.rowcount != 1:
                raise EditError(409, "REVISION_CONFLICT", "时间线已更新")
            self.insert_revision(db, rev)
            operation_id = uid()
            db.execute(insert(events).values(id=operation_id, workspace_id=workspace, edit_id=edit_id,
                       revision_id=rev["revision_id"], reason=body["reason"], operations=body["operations"],
                       created_at=rev["created_at"]))
            return dict(operation_id=operation_id, edit_project_id=edit_id, idempotency_key=body["idempotency_key"],
                        request_hash=digest(body), revision_id=rev["revision_id"], revision_no=rev["revision_no"],
                        clip_id_assignments=assignments)
        result, replayed = self.with_receipt(workspace, edit_id, "patch", body, save, guard)
        return dict(result, replayed=replayed)

    def enqueue(self, workspace, body, *, export_format="mp4", guard=None):
        revision_id = body["revision_id"]
        if export_format not in EXPORT_FORMATS:
            raise EditError(422, "UNSUPPORTED_FORMAT", "支持导出 MP4、WebM 和 MKV")
        if body["profile"] == "PREVIEW" and export_format != "mp4":
            raise EditError(422, "UNSUPPORTED_FORMAT", "预览使用 MP4，其他格式请选择导出")
        def save(db):
            rev = one(db, owned(revisions, workspace, revision_id)).snapshot
            profile = rev["output_profile"]
            fps = Fraction(profile["fps_num"], profile["fps_den"])
            if output_frame(rev["total_duration_us"], fps) == 0:
                raise EditError(422, "EMPTY_TIMELINE", "渲染至少需要一帧，请添加片段或延长选区")
            for source_id in {c["media_revision_id"] for c in rev["clips"]}:
                self.sources.media(workspace, source_id)
            slot = render_key(workspace, rev, body["profile"], caption_font(rev), export_format)
            existing = db.execute(select(jobs.c.id).where(jobs.c.workspace_id == workspace,
                                                          jobs.c.cache_slot == slot)).scalar_one_or_none()
            if existing:
                return existing
            job_id = uid()
            db.execute(insert(jobs).values(id=job_id, workspace_id=workspace, edit_id=rev["edit_project_id"],
                       revision_id=revision_id, profile=body["profile"], cache_key=slot, cache_slot=slot,
                       attempt=0, lease_until=0, status="QUEUED", created_at=timestamp(),
                       result=dict(renderer_version=RENDERER_VERSION, export_format=export_format, progress=0.0, error=None, artifact_id=None, validation_report_id=None,
                                   compiled_manifest_artifact_id=None, cancel_requested_at=None)))
            emit(db,uid(),workspace,job_id,"render.queued")
            return job_id
        request = body if export_format == "mp4" else dict(body, export_format=export_format)
        job_id, _ = self.with_receipt(workspace, workspace, "render", request, save, guard)
        return self.get(jobs, workspace, job_id, job_view)

    def cancel(self, workspace, job_id, body):
        def save(db):
            row = one(db, owned(jobs, workspace, job_id).with_for_update())
            if row.status in ("QUEUED", "RUNNING", "VALIDATING"):
                db.execute(update(jobs).where(jobs.c.id == job_id).values(
                    status="CANCELLED", cache_slot=None, attempt=row.attempt+1,
                    result=dict(row.result, cancel_requested_at=timestamp())))
                emit(db, uid(), workspace, job_id, "render.finished")
            return job_id
        self.with_receipt(workspace, job_id, "cancel", body, save)
        return self.get(jobs, workspace, job_id, job_view)
