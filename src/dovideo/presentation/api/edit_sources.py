"""HTTP entry points for importing and replaying editing sources."""
import hashlib
import hmac
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import create_engine, func, select
from starlette.concurrency import run_in_threadpool

from dovideo.application.edit_sources import EditSources
from dovideo.application.edit_timeline import missing
from dovideo.application.errors import InvalidMediaInput
from dovideo.application.media import normalize_video_filename
from dovideo.application.spec_contracts import validate_contract
from dovideo.infrastructure.upload_parts import UploadParts


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


@cache
def signing_key(root: Path):
    shared = os.getenv("DOVIDEO_MEDIA_SIGNING_KEY")
    if shared:
        if len(shared.encode("utf-8")) < 32:
            raise ValueError("DOVIDEO_MEDIA_SIGNING_KEY must contain at least 32 bytes")
        return shared.encode("utf-8")
    key = root / ".media-url-key"
    if not key.exists():
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as staged:
            staged.write(os.urandom(32))
        try:
            os.link(staged.name, key)
        except FileExistsError:
            pass
        finally:
            Path(staged.name).unlink()
    return key.read_bytes()


def signature(root, action, scope, resource, expires):
    return hmac.digest(signing_key(root), f"{action}|{scope}|{resource}|{expires}".encode(), "sha256").hex()


def install_source_routes(app, require_user, services, work_dir):
    infra = getattr(services, "infrastructure", None)
    if infra is not None and hasattr(infra, "engine"):
        root = Path(infra.settings.media_workspace) / "editing"
        engine = infra.engine
    else:
        root = Path(work_dir or getattr(getattr(services, "media", None), "root", "work/r1-media")) / "editing"
        engine = create_engine(f"sqlite:///{(root / 'editing.sqlite3').as_posix()}")
    store = app.state.edit_sources = EditSources(engine, root)
    from dovideo.infrastructure.r2_config import R2Infrastructure
    from dovideo.infrastructure.object_sources import SourceObjects, ObjectUploadParts
    if isinstance(infra, R2Infrastructure):
        store.objects = SourceObjects(infra.minio_client, infra.settings.minio_bucket)

    def parts_for(row):
        return ObjectUploadParts(store, row) if store.objects else UploadParts(store.root, row)

    app.state.edit_local_engine = engine if infra is None else None
    router = APIRouter(prefix="/v1")

    def workspace(user=Depends(require_user)):
        return store.workspace(user)

    def checked(name, body):
        try:
            validate_contract(name, body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def signed_url(request, name, scope, resource, expires):
        token = signature(store.root, name, scope, resource, expires)
        return str(request.url_for(name, resource=resource)) + "?" + urlencode(
            dict(scope=scope, expires=expires, token=token))

    def verify(name, scope, resource, expires, token):
        wanted = signature(store.root, name, scope, resource, expires)
        if expires <= time.time() or not hmac.compare_digest(wanted.encode(), token.encode()):
            raise HTTPException(403, "链接无效或已过期")

    @router.post("/projects", status_code=201)
    def create_project(body: dict, scope=Depends(workspace)):
        checked("ProjectCreate", body)
        if not body["title"].strip():
            raise HTTPException(422, "项目名不能为空")
        return store.create_project(scope, body["title"].strip())

    @router.get("/projects")
    def projects(cursor: str | None = None, limit: int = Query(20, ge=1, le=100), scope=Depends(workspace)):
        return store.page(scope, cursor, limit)

    @router.patch("/projects/{project_id}")
    def rename_project(project_id: UUID, body: dict, scope=Depends(workspace)):
        checked("ProjectCreate", body)
        title = body["title"].strip()
        if not title:
            raise HTTPException(422, "项目名不能为空")
        return store.rename_project(scope, str(project_id), title)

    @router.get("/projects/{project_id}/assets")
    def assets(project_id: UUID, cursor: str | None = None, limit: int = Query(20, ge=1, le=100), scope=Depends(workspace)):
        return store.page(scope, cursor, limit, str(project_id))

    def upload_session(request, row, numbers=None):
        layout = parts_for(row)
        parts = []
        for number in (numbers or range(1, layout.count + 1)):
            start, end = layout.bounds(number)
            url = signed_url(request, "put_source", row.workspace, row.upload, row.expires)
            headers = {}
            if layout.count > 1:
                # Bind each signed target to a single part number.
                params = dict(scope=row.workspace, expires=row.expires, part=number,
                    token=signature(store.root, f"put_part_{number}", row.workspace, row.upload, row.expires))
                url = str(request.url_for("put_source", resource=row.upload)) + "?" + urlencode(params)
                headers["Content-Range"] = f"bytes {start}-{end - 1}/{row.size}"
            parts.append(dict(part_number=number, headers=headers, url=url))
        mode = "MULTIPART" if layout.count > 1 else "SINGLE_PUT"
        return dict(upload_id=row.upload, asset_id=row.id, upload_mode=mode, expires_at=utc(row.expires), parts=parts)

    @router.post("/projects/{project_id}/assets", status_code=201)
    def create_upload(project_id: UUID, body: dict, request: Request, scope=Depends(workspace)):
        checked("UploadCreate", body)
        if ".." in body["filename"].replace("\\", "/").split("/"):
            raise HTTPException(422, "文件名不能包含上级目录")
        try:
            body["filename"] = normalize_video_filename(body["filename"])
        except InvalidMediaInput as exc:
            raise HTTPException(422, str(exc)) from exc
        if len(body["content_type"]) > 255:
            raise HTTPException(422, "文件类型过长")
        if body["size_bytes"] > 20 * 1024**3:
            raise HTTPException(413, "单个视频上限为 20 GiB")
        return upload_session(request, store.create_upload(scope, str(project_id), body))

    @router.put("/source-uploads/{resource}", name="put_source")
    async def put_source(resource: UUID, request: Request, scope: UUID, expires: int, token: str, part: int | None = None):
        verify("put_source" if part is None else f"put_part_{part}", scope, resource, expires, token)
        row = store.upload(str(scope), str(resource))
        layout = parts_for(row)
        if layout.count > 1 and part is None:
            raise HTTPException(422, "请按分片上传")
        if row.receipt:
            raise HTTPException(409, "上传已完成")
        start, end = layout.bounds(part or 1)
        total, digest = 0, hashlib.sha256()
        with tempfile.NamedTemporaryFile(dir=store.root, delete=False) as staged:
            path = Path(staged.name)
        try:
            with path.open("wb") as stream:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > end - start:
                        raise HTTPException(413, "上传内容超出声明大小")
                    digest.update(chunk)
                    await run_in_threadpool(stream.write, chunk)
            if part is None:
                await run_in_threadpool(store.receive, row, path, digest.hexdigest(), total)
            else:
                await run_in_threadpool(layout.save, part, path, digest.hexdigest(), total)
        finally:
            path.unlink(missing_ok=True)
        return Response(status_code=200, headers={"ETag": f'"{digest.hexdigest()}"'})

    @router.get("/uploads/{upload_id}")
    def upload_state(upload_id: UUID, scope=Depends(workspace)):
        row = store.upload(scope, str(upload_id))
        parts = [dict(part_number=1, etag=row.digest, size_bytes=row.size)] if row.digest else []
        layout = parts_for(row)
        if layout.count > 1:
            parts = layout.received()
        status = "COMPLETED" if row.receipt else "EXPIRED" if row.expires <= time.time() else "UPLOADING" if parts else "CREATED"
        return dict(upload_id=row.upload, asset_id=row.id, status=status, uploaded_parts=parts, expires_at=utc(row.expires))

    @router.post("/uploads/{upload_id}/complete", status_code=202)
    def complete(upload_id: UUID, body: dict, scope=Depends(workspace)):
        checked("UploadComplete", body)
        for part in body["parts"]:
            part["etag"] = part["etag"].removeprefix('"').removesuffix('"')
        row = store.upload(scope, str(upload_id))
        layout = parts_for(row)
        if layout.count > 1 and not row.receipt:
            if row.expires <= time.time():
                raise HTTPException(409, "上传已过期，请刷新会话")
            expected = layout.assemble(store, row, body["parts"])
            return store.complete(scope, row.upload, body, expected_parts=expected)
        return store.complete(scope, row.upload, body)

    @router.post("/uploads/{upload_id}/refresh")
    def refresh(upload_id: UUID, request: Request, body: dict, scope=Depends(workspace)):
        checked("UploadRefresh", body)
        return upload_session(request, store.renew(scope, str(upload_id)), body["part_numbers"])

    @router.get("/assets/{asset_id}")
    def asset(asset_id: UUID, scope=Depends(workspace)):
        return store.asset(scope, str(asset_id))

    @router.get("/media-revisions/{media_revision_id}")
    def media_revision(media_revision_id: UUID, scope=Depends(workspace)):
        return store.media(scope, str(media_revision_id))

    @router.post("/projects/{project_id}/imports/{legacy_media_id}", status_code=201)
    async def import_legacy(project_id: UUID, legacy_media_id: int, user=Depends(require_user)):
        scope = store.workspace(user)
        store.project(scope, str(project_id))
        record = await services.media.get(legacy_media_id)
        if record is None or record.user_id != user["id"]:
            raise missing()
        ticket = await services.playback_url(legacy_media_id, user["id"])
        token = parse_qs(urlparse(ticket).query)["access_token"][0]
        path, mime = await services.playback_file(legacy_media_id, token)
        try:
            return await run_in_threadpool(import_path, scope, str(project_id), Path(path), record, mime)
        finally:
            if hasattr(services, "release_response_file"):
                await services.release_response_file(path)

    def import_path(scope, project_id, path, record, mime):
        with tempfile.NamedTemporaryFile(dir=store.root, delete=False) as staged:
            temp = Path(staged.name)
        try:
            shutil.copyfile(path, temp)
            with temp.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            size = temp.stat().st_size
            upload = str(uuid5(UUID(project_id), f"{record.media_id}:{digest}"))
            row = store.create_upload(scope, project_id, dict(filename=record.filename, size_bytes=size,
                content_type=mime or "video/mp4", sha256=record.content_hash or digest), upload_id=upload)
            if row.receipt:
                return store.asset(scope, row.id)
            if row.expires <= time.time():
                row = store.renew(scope, upload)
            store.receive(row, temp, digest, size)
            return store.complete(scope, row.upload, dict(idempotency_key=row.upload,
                parts=[dict(part_number=1, etag=digest)]))
        finally:
            temp.unlink(missing_ok=True)

    @router.get("/artifacts/{artifact_id}")
    def artifact(artifact_id: UUID, request: Request, scope=Depends(workspace)):
        rid = str(artifact_id)
        row = store.artifact_row(scope, rid)
        if row is None:
            processing = getattr(app.state, "processing", None)
            derived = processing.catalog.get_record(scope, rid, "ARTIFACT") if processing else None
            if derived:
                expires = int(time.time()) + 900
                return dict(derived.body["artifact"], url=signed_url(request, "read_derived", scope, rid, expires),
                            url_expires_at=utc(expires))
            search = getattr(app.state, "evidence_search", None)
            if search and (found := search.artifact(scope, rid, request)):
                return found
            workflow = app.state.workflow_worker.artifact(scope, rid, request)
            return workflow or app.state.edit_worker.artifact(scope, rid, request)
        expires = int(time.time()) + 900
        mime = "video/mp4" if row.probe.get("browser_playable") else "application/octet-stream"
        return dict(artifact_id=rid, workspace_id=scope, kind="SOURCE", sha256=row.digest,
            size_bytes=row.size, mime_type=mime, media_revision_id=row.revision, timeline_revision_id=None,
            url=signed_url(request, "read_source", scope, rid, expires), url_expires_at=utc(expires))

    @router.post("/projects/{project_id}/evidence-previews")
    def previews(project_id: UUID, ids: list[UUID], request: Request, scope=Depends(workspace)):
        from dovideo.infrastructure.persistence.processing_tables import records

        store.project(scope, str(project_id))
        if len(ids) > 20:
            raise HTTPException(422, "一次最多预览 20 条证据")
        catalog = app.state.processing.catalog
        result = {}
        with store.sessions() as db:
            for eid in dict.fromkeys(map(str, ids)):
                row = catalog.get_record(scope, eid, "EVIDENCE")
                if row is None or row.body["evidence"]["project_id"] != str(project_id):
                    raise HTTPException(404, "证据不存在")
                frame = row.body.get("frame")
                if not frame:
                    anchor = db.execute(select(records.c.body).where(
                        records.c.run == row.run, records.c.kind == "EVIDENCE", records.c.unit.like("frame-%"),
                        records.c.start_us.between(row.start_us - 10_000_000, row.start_us + 10_000_000)
                    ).order_by(func.abs(records.c.start_us - row.start_us), records.c.start_us).limit(1)).scalar()
                    frame = anchor.get("frame") if anchor else None
                item = dict(text=row.body["evidence"]["claim"], source=row.body["modality"])
                if frame:
                    item.update(thumbnail=signed_url(request, "read_derived", scope, frame["artifact_id"],
                                                    int(time.time()) + 900), frame_us=frame["actual_us"])
                result[eid] = item
        return result

    @router.get("/source-files/{resource}", name="read_source")
    def read_source(resource: UUID, scope: UUID, expires: int, token: str):
        verify("read_source", scope, resource, expires, token)
        row = store.artifact_row(str(scope), str(resource))
        if row is None:
            raise HTTPException(404, "源文件不可用")
        mime = "video/mp4" if row.probe.get("browser_playable") else "application/octet-stream"
        return FileResponse(store.materialize(str(scope), row.digest), media_type=mime,
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control":"private, no-store"})

    @router.get("/derived-files/{resource}", name="read_derived")
    def read_derived(resource: UUID, scope: UUID, expires: int, token: str):
        verify("read_derived", scope, resource, expires, token)
        catalog = app.state.processing.catalog
        row = catalog.get_record(str(scope), str(resource), "ARTIFACT")
        if row is None:
            raise HTTPException(404, "产物不存在")
        return FileResponse(catalog.derived_path(str(scope), row.body["file"]), media_type=row.body["artifact"]["mime_type"],
                            headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff"})

    app.include_router(router)
