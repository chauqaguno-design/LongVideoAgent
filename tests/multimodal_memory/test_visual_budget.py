"""Exercise the remote protocol and accounting with no outgoing network connection."""

from uuid import uuid4

from fastapi import HTTPException
from PIL import Image
import pytest
from sqlalchemy import create_engine, select

from dovideo.application.edit_sources import EditSources
from dovideo.infrastructure.media import visual_observations as visual
from dovideo.infrastructure.persistence.media_visual_budget import billed_window
from dovideo.infrastructure.persistence.processing_catalog import MediaCatalog
from dovideo.infrastructure.persistence.processing_tables import records, runs
from dovideo.infrastructure.persistence.source_tables import projects, sources
from dovideo.infrastructure.persistence.sqlalchemy import Base
from dovideo.infrastructure.persistence.workspace_budget_table import workspace_budgets


def test_configured_deployment_opt_in_and_settlement(tmp_path, monkeypatch):
    assert visual.visual_deployment({}) is None
    assert visual.visual_deployment(dict(DOVIDEO_LOCAL_FLASH_INFERENCE="1")) is None
    local = dict(DOVIDEO_LOCAL_FLASH_INFERENCE="1", DOVIDEO_MODEL_BASE_URL="http://127.0.0.1:8000/v1")
    assert not visual.visual_deployment(local).billed
    cfg = dict(
        DOVIDEO_MODEL_BASE_URL="https://offline.example/v1",
        DOVIDEO_MODEL_MODEL="deepseek-flash",
        DOVIDEO_MODEL_VERSION="deepseek-flash-test",
        DOVIDEO_MODEL_API_KEY="fixture",
        DOVIDEO_MEDIA_REMOTE_FLASH="1",
        DOVIDEO_MEDIA_VISUAL_MAX_CNY_MICROUNITS="100",
        DOVIDEO_WORKSPACE_CNY_MAX_COST_MICROUNITS="100",
        DOVIDEO_FLASH_CNY_INPUT_MICROS_PER_MILLION="1000",
        DOVIDEO_FLASH_CNY_OUTPUT_MICROS_PER_MILLION="1000",
        DOVIDEO_FLASH_PRICE_VERSION="fixture",
    )
    deployment = visual.visual_deployment(cfg)
    assert deployment.billed and deployment.model == "deepseek-flash-test"
    assert visual.visual_deployment(dict(cfg, DOVIDEO_MODEL_VISION="0")) is None
    assert visual.visual_deployment(dict(cfg, DOVIDEO_MODEL_API_KEY="")) is None
    assert visual.visual_deployment(dict(cfg, DOVIDEO_MODEL_BASE_URL="http://offline.example/v1")) is None
    db = create_engine(f"sqlite:///{tmp_path/'budget.sqlite3'}")
    Base.metadata.create_all(db, tables=[projects, sources, runs, records, workspace_budgets])
    catalog = MediaCatalog(EditSources(db, tmp_path))
    scope = str(uuid4())
    project = catalog.sources.create_project(scope, "frame")["project_id"]
    upload = catalog.sources.create_upload(
        scope, project, dict(size_bytes=1, filename="source.mp4", content_type="video/mp4")
    )
    with catalog.write() as session:
        catalog.register(
            session,
            upload,
            dict(idempotency_key="acceptance", requested_capabilities=["visual_observation"]),
        )
    run = catalog.claim()
    image = tmp_path / "frame.jpg"
    Image.new("RGB", (40, 40)).save(image)
    usage = dict(prompt_tokens=50, completion_tokens=12, total_tokens=62)
    response = dict(
        model=deployment.model, usage=usage, choices=[dict(message=dict(content='{"observations":[]}'))]
    )
    calls = []

    def transport(config, payload):
        calls.append(len(payload))
        return response

    monkeypatch.setattr(visual, "post", transport)

    def observe(unit, env):
        def charge(step, size, n, send):
            return billed_window(catalog, run, f"{unit}:{step}", size, n, env, send)

        return visual.observe(deployment, [("frame", 0, image)], charge)

    assert observe("first", cfg)[1]["usage"] == usage
    assert len(calls) == 1 and catalog.visual_usage(scope, run.id)["committed_microunits"] == 2
    with pytest.raises(ValueError, match="已经发起"):
        observe("first", cfg)
    with pytest.raises(ValueError, match="超过可用预算"):
        observe("second", dict(cfg, DOVIDEO_WORKSPACE_CNY_MAX_COST_MICROUNITS="1"))
    assert len(calls) == 1

    responses = iter([dict(response, choices=[dict(message=dict(content="invalid"))]), response])
    monkeypatch.setattr(visual, "post", lambda config, payload: next(responses))
    _, repaired = observe("repairable", cfg)
    assert [a["step"] for a in repaired["attempts"]] == ["observe", "repair"]

    def disconnect(config, payload):
        raise OSError("response lost")

    monkeypatch.setattr(visual, "post", disconnect)
    with pytest.raises(OSError):
        observe("third", cfg)
    usage = catalog.visual_usage(scope, run.id)
    assert usage["model_calls"] == 4 and usage["uncertain_microunits"] > 0
    with pytest.raises(HTTPException):
        catalog.visual_usage(str(uuid4()), run.id)
    with catalog.sessions() as session:
        assert (
            session.scalar(select(workspace_budgets.c.exposure))
            == usage["committed_microunits"] + usage["uncertain_microunits"]
        )
