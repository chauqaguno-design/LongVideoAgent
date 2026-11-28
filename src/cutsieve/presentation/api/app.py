"""CutSieve HTTP API."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from cutsieve.infrastructure.persistence.sqlalchemy import Base

from .runtime import R1ServiceError


@dataclass(frozen=True, slots=True)
class ApiSettings:
    """Small environment-driven presentation configuration."""

    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )
    title: str = "CutSieve API"

    @classmethod
    def from_environment(cls) -> "ApiSettings":
        raw = os.environ.get("CUTSIEVE_CORS_ORIGINS", "")
        if not raw.strip():
            return cls()
        origins = tuple(item.strip() for item in raw.split(",") if item.strip())
        return cls(cors_origins=origins or cls().cors_origins)


def load_local_dotenv(
    dotenv_path: Path | None = None,
    environ: dict[str, str] | None = None,
) -> Path | None:
    """Load simple KEY=VALUE pairs from a local .env without overriding set vars."""
    target = os.environ if environ is None else environ
    candidates = [dotenv_path] if dotenv_path is not None else [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[4] / ".env",
    ]
    chosen = next((path for path in candidates if path is not None and path.is_file()), None)
    if chosen is None:
        return None
    for raw_line in chosen.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        target.setdefault(key, value)
    return chosen


def local_workspace_user() -> dict[str, int]:
    # Preserve the workspace associated with the original first local account.
    return {"id": 1}


def create_app(
    *,
    settings: ApiSettings | None = None,
    work_dir: Path | None = None,
    agent_transport: object | None = None,
) -> FastAPI:
    if (
        work_dir is None
        and not os.environ.get("PYTEST_CURRENT_TEST")
        and os.environ.get("CUTSIEVE_DISABLE_DOTENV") != "1"
    ):
        load_local_dotenv()
    selected_settings = settings or ApiSettings.from_environment()
    local_root = Path(work_dir or os.getenv("CUTSIEVE_R1_MEDIA_DIR", "work/r1-media"))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        engine = _app.state.edit_local_engine
        try:
            _app.state.edit_sources.root.mkdir(parents=True, exist_ok=True)
            tables = [t for t in Base.metadata.sorted_tables if t.name.startswith("vm_")]
            with engine.begin() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                Base.metadata.create_all(connection, tables=tables)
            await _app.state.edit_worker.start()
            await _app.state.processing.start()
            await _app.state.workflow_worker.start()
            await _app.state.work_outbox.start()
            yield
        finally:
            await _app.state.work_outbox.stop()
            await _app.state.workflow_worker.stop()
            await _app.state.processing.stop()
            await _app.state.edit_worker.stop()
            _app.state.evidence_search.close()
            engine.dispose()

    app = FastAPI(title=selected_settings.title, version="0.1.0", lifespan=lifespan)
    app.state.settings = selected_settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(selected_settings.cors_origins),
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Accept", "Idempotency-Key", "Last-Event-ID", "Content-Range"],
        expose_headers=["ETag"],
    )

    @app.exception_handler(R1ServiceError)
    async def r1_error_handler(_request: Request, exc: R1ServiceError) -> JSONResponse:
        if _request.url.path.startswith("/v1/"):
            return _v1_error(exc.status_code, exc.message)
        return _error(exc.message, exc.status_code, exc.code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        if _request.url.path.startswith("/v1/"):
            return _v1_error(422, "请求参数无效")
        # Validation details may contain input values; the public API keeps a
        # stable, non-sensitive message instead of echoing them.
        return _error("请求参数无效", 400, 400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if _request.url.path.startswith("/v1/"):
            return _v1_error(exc.status_code, str(exc.detail))
        message = {
            404: "资源不存在",
            405: "请求方法不支持",
        }.get(exc.status_code, "请求失败")
        return _error(message, exc.status_code, exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
        # Never expose stack traces, filesystem paths, provider payloads, or
        # configuration values through the HTTP error contract.
        return _error("服务暂不可用", 500, 500)

    from .edit_sources import install_source_routes
    from .editing import install_edit_routes
    from .capabilities import install_caps
    from .media_processing import install_processing
    from .search import install_search_routes
    from .workflows import install_workflow_routes
    install_source_routes(app, local_workspace_user, local_root)
    install_edit_routes(app, local_workspace_user)
    install_processing(app, local_workspace_user)
    install_search_routes(app, local_workspace_user)
    install_workflow_routes(app, local_workspace_user, transport=agent_transport)
    install_caps(app, local_workspace_user)

    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        return _ok({"service": "CutSieve", "api": "v1"})

    @app.get("/health")
    async def health() -> JSONResponse:
        return _ok("ok")

    return app


def _v1_error(status, message):
    return JSONResponse(dict(code=f"HTTP_{status}", message=message, retryable=status >= 500,
                             request_id=str(uuid4()), details={}), status_code=status)


def _ok(data: Any = None, message: str = "", status_code: int = 200) -> JSONResponse:
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json", by_alias=True)
    return JSONResponse({"code": 0, "message": message, "data": data}, status_code=status_code)


def _error(
    message: str,
    status_code: int,
    code: int | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": status_code if code is None else code, "message": message, "data": None},
        headers=headers,
    )
