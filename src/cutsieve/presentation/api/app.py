"""CutSieve HTTP API."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from cutsieve.infrastructure.persistence.sqlalchemy import Base

from .runtime import LocalAuthService, R1ServiceError
from .schemas import LoginRequest, RegisterRequest


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


def create_app(
    *,
    settings: ApiSettings | None = None,
    work_dir: Path | None = None,
) -> FastAPI:
    selected_settings = settings or ApiSettings.from_environment()
    local_root = Path(work_dir or os.getenv("CUTSIEVE_R1_MEDIA_DIR", "work/r1-media"))
    auth = LocalAuthService(local_root / "local-accounts.sqlite3")

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
    app.state.auth = auth
    app.state.settings = selected_settings
    allow_credentials = "*" not in selected_settings.cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(selected_settings.cors_origins),
        allow_credentials=allow_credentials,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", "Idempotency-Key", "Last-Event-ID", "Content-Range"],
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

    def require_user(request: Request) -> dict[str, Any]:
        return auth.require(request.headers.get("authorization"))

    from .edit_sources import install_source_routes
    from .editing import install_edit_routes
    from .capabilities import install_caps
    from .media_processing import install_processing
    from .search import install_search_routes
    from .workflows import install_workflow_routes
    install_source_routes(app, require_user, local_root)
    install_edit_routes(app, require_user)
    install_processing(app, require_user)
    install_search_routes(app, require_user)
    install_workflow_routes(app, require_user)
    install_caps(app, require_user)

    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        return _ok({"service": "CutSieve", "api": "v1"})

    @app.get("/health")
    async def health() -> JSONResponse:
        return _ok("ok")

    @app.post("/user/register")
    async def register(payload: RegisterRequest) -> JSONResponse:
        data = auth.register(
            payload.username,
            payload.password,
            payload.nickname,
        )
        return _ok(data, "注册成功")

    @app.post("/user/login")
    async def login(payload: LoginRequest) -> JSONResponse:
        data = auth.login(payload.username, payload.password)
        return _ok(data, "登录成功")

    @app.post("/user/logout")
    async def logout(request: Request, _user: dict[str, Any] = Depends(require_user)) -> JSONResponse:
        auth.logout(request.headers.get("authorization"))
        return _ok(None, "退出成功")

    @app.get("/user/me")
    async def current_user(user: dict[str, Any] = Depends(require_user)) -> JSONResponse:
        return _ok(user)

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
