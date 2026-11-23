"""Project-scoped access to immutable media for the editing workspace."""
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from dovideo.infrastructure.persistence.source_tables import projects, sources


def workspace_id(user_id: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"videomind:personal-workspace:{user_id}"))


def project_view(row):
    return dict(project_id=row.id, workspace_id=row.workspace, title=row.title, created_at=row.created)


def asset_view(row):
    probe = row.probe or {}
    return dict(asset_id=row.id, workspace_id=row.workspace, project_id=row.project,
                media_revision_id=row.revision, filename=row.filename, sha256=row.digest or "",
                size_bytes=row.size, duration_us=probe.get("duration_us"),
                status="AVAILABLE" if row.receipt else "UPLOADING", capabilities=probe.get("capabilities", []),
                source_artifact_id=row.artifact if row.receipt else None,
                playback_artifact_id=row.artifact if probe.get("browser_playable") else probe.get("playback_artifact_id"))


class EditSources:
    def __init__(self, engine, root: Path):
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.root = root.resolve()
        self.enqueue_completed = None
        self.objects = None

    def workspace(self, user):
        return workspace_id(int(user["id"]))

    def _one(self, table, scope, column, key):
        query = select(table).where(table.c.workspace == scope, column == key)
        if table is sources:
            query = query.where(func.coalesce(sources.c.probe["deleted"].as_boolean(), False) == False)
        with self.sessions() as session:
            row = session.execute(query).first()
        if row is None:
            raise HTTPException(404, "资源不存在")
        return row

    def project(self, scope, project_id):
        return project_view(self._one(projects, scope, projects.c.id, project_id))

    def create_project(self, scope, title):
        row = dict(id=str(uuid4()), workspace=scope, title=title,
                   created=datetime.now(timezone.utc).isoformat())
        with self.sessions.begin() as session:
            session.execute(projects.insert().values(**row))
        return self.project(scope, row["id"])

    def rename_project(self, scope, project_id, title):
        with self.sessions.begin() as session:
            changed = session.execute(projects.update().where(
                projects.c.workspace == scope, projects.c.id == project_id).values(title=title))
            if not changed.rowcount:
                raise HTTPException(404, "项目不存在")
        return self.project(scope, project_id)

    def page(self, scope, cursor, limit, project_id=None):
        table, view = (projects, project_view) if project_id is None else (sources, asset_view)
        query = select(table).where(table.c.workspace == scope)
        if project_id is not None:
            self.project(scope, project_id)
            query = query.where(sources.c.project == project_id)
            query = query.where(func.coalesce(sources.c.probe["deleted"].as_boolean(), False) == False)
        if cursor:
            query = query.where(table.c.id > cursor)
        with self.sessions() as session:
            rows = session.execute(query.order_by(table.c.id).limit(limit + 1)).all()
        return dict(items=[view(row) for row in rows[:limit]],
                    next_cursor=rows[limit-1].id if len(rows) > limit else None)

    def create_upload(self, scope, project_id, body, *, upload_id=None):
        self.project(scope, project_id)
        ids = {name: str(uuid4()) for name in ("id", "artifact", "revision", "upload")}
        if upload_id:
            ids["upload"] = upload_id
        try:
            with self.sessions.begin() as session:
                session.execute(sources.insert().values(**ids, workspace=scope, project=project_id,
                    filename=body["filename"], size=body["size_bytes"], mime=body["content_type"],
                    expected_digest=body.get("sha256"), expires=int(time.time()) + 3600))
        except IntegrityError:
            # Concurrent imports of the same immutable legacy source share one upload.
            if not upload_id:
                raise
        return self.upload(scope, ids["upload"])

    def upload(self, scope, upload_id):
        return self._one(sources, scope, sources.c.upload, upload_id)

    def renew(self, scope, upload_id):
        row = self.upload(scope, upload_id)
        if row.receipt:
            raise HTTPException(409, "上传已完成")
        if row.expires <= time.time():
            with self.sessions.begin() as session:
                session.execute(sources.update().where(sources.c.id == row.id,
                    sources.c.expires == row.expires, sources.c.receipt.is_(None)
                ).values(expires=int(time.time()) + 3600))
        return self.upload(scope, upload_id)

    def asset(self, scope, asset_id):
        return asset_view(self._one(sources, scope, sources.c.id, asset_id))

    def media(self, scope, revision_id):
        row = self._one(sources, scope, sources.c.revision, revision_id)
        if not row.receipt:
            raise HTTPException(409, "媒体尚未完成探测")
        return dict(media_revision_id=row.revision, workspace_id=scope, asset_id=row.id,
            sha256=row.digest, source_artifact_id=row.artifact,
            playback_artifact_id=asset_view(row)["playback_artifact_id"],
            proxy_time_mapping_artifact_id=row.probe.get("proxy_time_mapping_artifact_id"),
            **{key: row.probe[key] for key in ("duration_us", "streams", "source_origin")})

    def blob(self, scope, digest):
        return self.root / "sources" / scope / digest

    def source_path(self, scope, revision_id):
        return self.materialize(scope, self.media(scope, revision_id)["sha256"])

    def materialize(self, scope, digest):
        path = self.blob(scope, digest)
        return self.objects.materialize(scope, digest, path) if self.objects else path

    def require_readable(self, session, scope, revision_ids):
        """Hold source rows through publication so deletion and publication have a defined order."""
        ids = set(revision_ids)
        query = select(sources.c.revision, sources.c.probe, sources.c.receipt).where(
            sources.c.workspace == scope, sources.c.revision.in_(ids)).order_by(sources.c.revision).with_for_update()
        available = {r.revision for r in session.execute(query) if r.receipt and not r.probe.get("deleted")}
        if ids != available:
            raise HTTPException(404, "源素材已不可用")

    def display_size(self, scope, revision_id):
        probe = self._one(sources, scope, sources.c.revision, revision_id).probe
        if not probe:
            raise HTTPException(409, "媒体尚未完成探测")
        return probe["width"], probe["height"]

    def receive(self, row, staged, digest, size):
        if size != row.size or (row.expected_digest and row.expected_digest != digest):
            raise HTTPException(422, "文件大小或 SHA-256 与声明不符")
        if row.digest and row.digest != digest:
            raise HTTPException(409, "此上传已绑定其他内容")
        path = self.blob(row.workspace, digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.sessions.begin() as session:
            changed = session.execute(sources.update().where(sources.c.id == row.id,
                func.coalesce(sources.c.probe["deleted"].as_boolean(), False) == False,
                or_(sources.c.digest.is_(None), sources.c.digest == digest)).values(digest=digest))
            if changed.rowcount == 0:
                raise HTTPException(409, "此上传已绑定其他内容")
            if self.objects:
                self.objects.put(self.objects.source_key(row.workspace, digest), staged)
            try:
                os.link(staged, path)
            except FileExistsError:
                pass  # The same bytes have already been stored in this workspace.

    def complete(self, scope, upload_id, body, *, expected_parts=None):
        from dovideo.infrastructure.edit_media import check_source_frame, probe_media
        from dovideo.presentation.composition import AnalysisSettings

        row = self.upload(scope, upload_id)
        receipt = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if row.receipt == receipt:
            return asset_view(row)
        if row.receipt:
            raise HTTPException(409, "此上传已用另一请求完成")
        if row.expires <= time.time():
            raise HTTPException(409, "上传会话已过期")
        if expected_parts is None:
            expected_parts = [{"part_number": 1, "etag": row.digest}]
        if not row.digest or body.get("sha256", row.digest) != row.digest or body["parts"] != expected_parts:
            raise HTTPException(422, "分片凭据与已上传文件不符")
        try:
            settings = AnalysisSettings.from_environment()
            path = self.materialize(scope, row.digest)
            probe = probe_media(path, ffprobe=settings.ffprobe_executable)
            duration_limit = int(os.getenv("DOVIDEO_MAX_MEDIA_DURATION_SECONDS", "86400"))
            if not 0 < probe["duration_us"] <= duration_limit * 1_000_000 or probe["width"] * probe["height"] > 40_000_000:
                raise HTTPException(422, "视频时长或画面尺寸超出本服务的处理范围")
            check_source_frame(path, probe["video_index"], settings.ffmpeg_executable)
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, "无法读取视频轨道或时长，请检查源文件") from exc
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(503, "媒体探测暂时不可用，可以稍后重试") from exc
        with self.sessions.begin() as session:
            changed = session.execute(sources.update().where(sources.c.id == row.id,
                sources.c.receipt.is_(None),
                func.coalesce(sources.c.probe["deleted"].as_boolean(), False) == False).values(probe=probe, receipt=receipt))
            if changed.rowcount == 0:
                previous = session.execute(select(sources).where(sources.c.id==row.id).with_for_update()).one()
                if previous.receipt==receipt and not (previous.probe or {}).get("deleted"):
                    return asset_view(previous)
                raise HTTPException(409, "上传已结束")
            if self.enqueue_completed:
                source = session.execute(select(sources).where(sources.c.id == row.id)).one()
                self.enqueue_completed(session, source)
        return self.asset(scope, row.id)

    def artifact_row(self, scope, artifact_id):
        with self.sessions() as session:
            return session.execute(select(sources).where(sources.c.workspace == scope,
                sources.c.artifact == artifact_id, sources.c.receipt.is_not(None),
                func.coalesce(sources.c.probe["deleted"].as_boolean(), False) == False)).first()
