"""Project-scoped evidence search and screenshot uploads."""

import hmac
from io import BytesIO
import json
import time
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from starlette.formparsers import MultiPartException, MultiPartParser

from cutsieve.application.evidence_search import EvidenceSearch
from cutsieve.application.spec_contracts import SearchRequest
from cutsieve.infrastructure.search_features import MAX_IMAGE_BYTES, read_query_image
from .edit_sources import signature, utc
from .schemas import Crop


def install_search_routes(app, workspace_user):
    sources = app.state.edit_sources
    from cutsieve.infrastructure.search_encoder import local_encoder

    search = app.state.evidence_search = EvidenceSearch(
        sources, app.state.processing.catalog, local_encoder(sources.root)
    )
    app.state.processing.catalog.prepare_search_rows = search.prepare_rows
    router = APIRouter(prefix="/v1")

    def workspace(user=Depends(workspace_user)):
        return sources.workspace(user)

    def artifact(scope, key, request):
        row = search.files.get(scope, key)
        if not row or row.kind not in {"IMAGE_QUERY", "QUERY_RESULT"}:
            return None
        expiry = int(time.time()) + 300
        token = signature(sources.root, "search_file", scope, row.id, expiry)
        url = request.url_for("search_file", resource=row.id).include_query_params(
            w=scope, expires=expiry, sig=token
        )
        return dict(
            artifact_id=row.id,
            workspace_id=scope,
            kind=row.kind,
            sha256=row.digest,
            size_bytes=row.size,
            mime_type=row.mime,
            media_revision_id=None,
            timeline_revision_id=None,
            url=str(url),
            url_expires_at=utc(expiry),
        )

    search.artifact = artifact

    @router.post("/projects/{project_id}/query-images", status_code=201)
    async def query_image(project_id: UUID, request: Request, scope=Depends(workspace)):
        project = str(project_id)
        sources.project(scope, project)

        async def limited_stream():
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_IMAGE_BYTES + 65536:
                    raise MultiPartException("图片超出 8 MiB")
                yield chunk

        try:
            form = await MultiPartParser(request.headers, limited_stream(), max_files=1, max_fields=0).parse()
        except MultiPartException as exc:
            raise HTTPException(413 if "8 MiB" in exc.message else 422, exc.message) from exc
        try:
            upload = form.get("file")
            if upload is None:
                raise HTTPException(422, "请选择图片")
            data = await upload.read(MAX_IMAGE_BYTES + 1)
        finally:
            await form.close()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "图片超出 8 MiB")
        try:
            mime, _ = read_query_image(data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        key = search.files.save(scope, project, data, "IMAGE_QUERY", mime)
        return artifact(scope, key, request)

    @router.post("/projects/{project_id}/query-images/{image_id}/roi", status_code=201)
    def crop_query(
        project_id: UUID,
        image_id: UUID,
        body: Crop,
        request: Request,
        scope=Depends(workspace),
    ):
        x, y, w, h = (body[k] for k in ("x", "y", "width", "height"))
        project = str(project_id)
        data = search.files.image(scope, project, str(image_id))
        _, original = read_query_image(data)
        if x + w > original.width or y + h > original.height:
            raise HTTPException(422, "裁图范围超出原图")
        png = BytesIO()
        original.crop((x, y, x + w, y + h)).save(png, "PNG")
        if png.tell() > MAX_IMAGE_BYTES:
            raise HTTPException(413, "裁剪结果超过 8 MiB，请减小区域")
        child = str(uuid5(image_id, f"roi:{x}:{y}:{w}:{h}"))
        meta = {
            "parent_artifact_id": str(image_id),
            "roi": body,
            "original_size": list(original.size),
        }
        search.files.save(scope, project, png.getvalue(), "IMAGE_QUERY", "image/png", ident=child)
        search.files.save(
            scope,
            project,
            json.dumps(meta, sort_keys=True).encode(),
            "QUERY_IMAGE_META",
            "application/json",
            ident=str(uuid5(UUID(child), "roi-provenance")),
        )
        return artifact(scope, child, request)

    @router.post("/search")
    def find(body: SearchRequest, scope=Depends(workspace)):
        return search.search(scope, body)

    @router.get("/projects/{project_id}/query-results/{artifact_id}/occurrences")
    def occurrences(
        project_id: UUID,
        artifact_id: UUID,
        cursor: UUID | None = None,
        limit: int = Query(20, ge=1, le=100),
        scope=Depends(workspace),
    ):
        events = search.files.occurrences(scope, str(project_id), str(artifact_id))
        start = 0
        if cursor:
            start = next(
                (i + 1 for i, e in enumerate(events) if e["occurrence_id"] == str(cursor)),
                -1,
            )
            if start < 0:
                raise HTTPException(422, "游标不属于此核查清单")
        page = events[start : start + limit]
        return dict(
            items=page,
            next_cursor=page[-1]["occurrence_id"] if start + limit < len(events) else None,
        )

    @router.get("/search-files/{resource}", name="search_file")
    def download(resource: UUID, w: UUID, expires: int, sig: str):
        scope, key = str(w), str(resource)
        expected = signature(sources.root, "search_file", scope, key, expires)
        if expires <= time.time() or not hmac.compare_digest(expected.encode(), sig.encode()):
            raise HTTPException(403, "链接已失效")
        row = search.files.get(scope, key)
        if not row or row.kind not in {"IMAGE_QUERY", "QUERY_RESULT"}:
            raise HTTPException(404, "文件不存在")
        return FileResponse(
            search.files.path(row),
            media_type=row.mime,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    app.include_router(router)
