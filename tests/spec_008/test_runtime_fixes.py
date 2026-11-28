"""Retain local configuration and media processing regressions."""

from base64 import b64encode
import json

import pytest
from sqlalchemy import create_engine

from cutsieve.application.edit_sources import EditSources
from cutsieve.application.edit_timeline import uid
from cutsieve.infrastructure.persistence.edit_tables import EDIT_TABLES
from cutsieve.infrastructure.persistence.media_visual_budget import billed_window
from cutsieve.infrastructure.persistence.processing_catalog import MediaCatalog
from cutsieve.infrastructure.persistence.processing_tables import records, runs as processing_runs
from cutsieve.infrastructure.persistence.source_tables import projects, sources
from cutsieve.infrastructure.persistence.sqlalchemy import Base
from cutsieve.infrastructure.persistence.workspace_budget_table import workspace_budgets
from cutsieve.presentation.api.app import load_local_dotenv


@pytest.mark.parametrize("large_payload", [False, True])
def test_visual_window_caps_text_reservation(tmp_path, large_payload):
    engine = create_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
    Base.metadata.create_all(engine, tables=[projects, sources, *EDIT_TABLES, processing_runs, records, workspace_budgets])
    catalog = MediaCatalog(EditSources(engine, tmp_path))
    workspace = uid()
    project = catalog.sources.create_project(workspace, "Frames")["project_id"]
    upload = catalog.sources.create_upload(
        workspace, project, dict(filename="source.mp4", size_bytes=1, content_type="video/mp4")
    )
    with catalog.write() as db:
        catalog.register(db, upload, dict(idempotency_key=uid(), requested_capabilities=["visual_observation"]))
    run = catalog.claim()
    image = b64encode(b"frame" * 40_000).decode("ascii")
    payload_bytes = len(json.dumps(dict(image_url=image)).encode()) if large_payload else 200
    expected_text = 4096 if large_payload else 200
    env = dict(
        CUTSIEVE_FLASH_PRICE_VERSION="fixture",
        CUTSIEVE_FLASH_CNY_INPUT_MICROS_PER_MILLION="1000000",
        CUTSIEVE_FLASH_CNY_OUTPUT_MICROS_PER_MILLION="1000000",
        CUTSIEVE_WORKSPACE_CNY_MAX_COST_MICROUNITS="9000",
        CUTSIEVE_MEDIA_VISUAL_MAX_CNY_MICROUNITS="9000",
    )
    response = dict(model="deepseek-chat")
    assert billed_window(catalog, run, "observe", payload_bytes, 1, env, lambda: response) == response
    usage = catalog.visual_usage(workspace, run.id)
    assert usage["uncertain_microunits"] == expected_text + 2048 + 768
    engine.dispose()


def test_load_local_dotenv_parses_values_without_overriding_existing_env(tmp_path):
    env_file = tmp_path / ".env.fixture"
    env_file.write_text(
        "# comment\n"
        "CUTSIEVE_MODEL_MODEL=deepseek-v4-flash # inline note\n"
        "CUTSIEVE_MODEL_BASE_URL='https://api.deepseek.com/v1'\n"
        'CUTSIEVE_FFMPEG_DIR="C:/tools/ffmpeg/bin"\n'
        "CUTSIEVE_KEEP=from-file\n",
        encoding="utf-8",
    )
    target = {"CUTSIEVE_KEEP": "from-shell"}
    assert load_local_dotenv(env_file, target) == env_file
    assert target == {
        "CUTSIEVE_KEEP": "from-shell",
        "CUTSIEVE_MODEL_MODEL": "deepseek-v4-flash",
        "CUTSIEVE_MODEL_BASE_URL": "https://api.deepseek.com/v1",
        "CUTSIEVE_FFMPEG_DIR": "C:/tools/ffmpeg/bin",
    }
