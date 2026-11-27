from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from cutsieve.application.edit_sources import workspace_id
from cutsieve.presentation.api import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(work_dir=tmp_path / "media")) as test_client:
        yield test_client


def test_health_envelope(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"code": 0, "message": "", "data": "ok"}


def test_local_workspace_uses_existing_scope_and_wal(client):
    with client.app.state.edit_local_engine.connect() as db:
        assert db.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert db.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000

    created = client.post("/v1/projects", json={"title": "Local project"})
    assert created.status_code == 201
    assert created.json()["workspace_id"] == workspace_id(1)
    projects = client.get("/v1/projects")
    assert projects.status_code == 200
    assert projects.json()["items"] == [created.json()]


def test_capabilities_uses_local_defaults_and_environment(client, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("CUTSIEVE_MODEL") or key in ("CUTSIEVE_BASE_URL", "CUTSIEVE_DEEPSEEK_BASE_URL"):
            monkeypatch.delenv(key)
    local = client.get("/v1/capabilities").json()
    assert not any(local[key] for key in ("text", "vision", "native_tools", "structured_json"))

    monkeypatch.setenv("CUTSIEVE_MODEL_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("CUTSIEVE_MODEL_MODEL", "local-vlm")
    configured = client.get("/v1/capabilities").json()
    assert configured["resolved_model_version"] == "local-vlm"
    assert configured["text"] and configured["vision"] and configured["structured_json"]
    assert not configured["native_tools"]
