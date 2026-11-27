from __future__ import annotations

from contextlib import closing
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from dovideo.presentation.api import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(work_dir=tmp_path / "media")) as test_client:
        yield test_client


def auth_headers(client: TestClient) -> dict[str, str]:
    registration = client.post(
        "/user/register",
        json={"username": "r1_user", "password": "password123", "nickname": "R1"},
    )
    assert registration.status_code == 200
    login = client.post(
        "/user/login",
        json={"username": "r1_user", "password": "password123"},
    )
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['data']['token']}"}


def test_health_envelope_and_auth_error_do_not_leak_details(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"code": 0, "message": "", "data": "ok"}

    denied = client.get("/user/me")
    assert denied.status_code == 401
    body = denied.json()
    assert body["data"] is None
    assert "traceback" not in body["message"].lower()
    assert "token" not in body["message"].lower()


def test_registration_login_and_revocation(client):
    headers = auth_headers(client)
    with client.app.state.edit_local_engine.connect() as db:
        assert db.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert db.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
    with closing(sqlite3.connect(client.app.state.auth.accounts)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    me = client.get("/user/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["data"]["username"] == "r1_user"
    assert client.get("/v1/projects", headers=headers).status_code == 200
    assert client.post("/user/logout", headers=headers).status_code == 200
    assert client.get("/user/me", headers=headers).status_code == 401


def test_capabilities_uses_local_defaults_and_environment(client, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("DOVIDEO_MODEL") or key in ("DOVIDEO_BASE_URL", "DOVIDEO_DEEPSEEK_BASE_URL"):
            monkeypatch.delenv(key)
    assert client.get("/v1/capabilities").status_code == 401
    headers = auth_headers(client)
    local = client.get("/v1/capabilities", headers=headers).json()
    assert not any(local[key] for key in ("text", "vision", "native_tools", "structured_json"))

    monkeypatch.setenv("DOVIDEO_MODEL_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("DOVIDEO_MODEL_MODEL", "local-vlm")
    configured = client.get("/v1/capabilities", headers=headers).json()
    assert configured["resolved_model_version"] == "local-vlm"
    assert configured["text"] and configured["vision"] and configured["structured_json"]
    assert not configured["native_tools"]
