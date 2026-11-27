"""Regressions for model budgets, the Flash adapter, and sampled output review."""

import asyncio
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
import json
from math import ceil
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select

from cutsieve.application.edit_sources import EditSources
from cutsieve.application.edit_timeline import EditError, uid
from cutsieve.application.workflow_identity import completion_tokens_for, context_estimate
from cutsieve.application.workflow_review import render_document, repair_for, review_issue, review_report
from cutsieve.application.workflow_state import DEFAULT_BUDGET, new_state
from cutsieve.infrastructure.persistence.edit_repository import EditRepository
from cutsieve.infrastructure.persistence.edit_tables import EDIT_TABLES
from cutsieve.infrastructure.persistence.media_visual_budget import billed_window
from cutsieve.infrastructure.persistence.processing_catalog import MediaCatalog
from cutsieve.infrastructure.persistence.processing_tables import records, runs as processing_runs
from cutsieve.infrastructure.persistence.source_tables import projects, sources
from cutsieve.infrastructure.persistence.sqlalchemy import Base
from cutsieve.infrastructure.persistence.workflow_budget import run_model
from cutsieve.infrastructure.persistence.workflow_repository import WorkflowRepository
from cutsieve.infrastructure.persistence.workflow_tables import WORKFLOW_TABLES, model_requests
from cutsieve.infrastructure.providers.workflow_flash import FlashPlanner


class RecordingTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.loops = []
        self.payloads = []

    async def post(self, url, *, headers, json, timeout):
        self.loops.append(asyncio.get_running_loop())
        self.payloads.append(json)
        return next(self.responses)


@pytest.fixture
def model_env():
    return dict(
        CUTSIEVE_MODEL_BASE_URL="http://127.0.0.1:8000/v1",
        CUTSIEVE_MODEL_MODEL="deepseek/deepseek-chat",
        CUTSIEVE_WORKFLOW_LIVE_MODEL="1",
        CUTSIEVE_FLASH_PRICE_VERSION="fixture",
        CUTSIEVE_FLASH_CNY_INPUT_MICROS_PER_MILLION="1000000",
        CUTSIEVE_FLASH_CNY_OUTPUT_MICROS_PER_MILLION="1000000",
        CUTSIEVE_WORKSPACE_CNY_MAX_COST_MICROUNITS="9000",
        CUTSIEVE_MEDIA_VISUAL_MAX_CNY_MICROUNITS="9000",
    )


@pytest.fixture
def source_store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
    Base.metadata.create_all(
        engine, tables=[projects, sources, *EDIT_TABLES, *WORKFLOW_TABLES, processing_runs, records]
    )
    yield EditSources(engine, tmp_path)
    engine.dispose()


@pytest.fixture
def visual_messages():
    encoded = b64encode(b"frame" * 40_000).decode("ascii")
    return [dict(role="user", content=[
        dict(type="text", text="检查画面"),
        dict(type="image_url", image_url={"url": "data:image/jpeg;base64," + encoded}),
    ])]


@pytest.mark.parametrize(("char", "tokens_per_char"), [("剪", 1.2), ("é", 1.2), ("a", 1 / 3.5)])
def test_context_estimate_uses_character_rates(char, tokens_per_char):
    tools = [dict(name="find_clip")]
    empty = [dict(role="user", content="")]
    overhead = len(json.dumps([empty, tools], ensure_ascii=False)) / 3.5
    messages = [dict(role="user", content=char * 700)]
    estimate = context_estimate(messages, tools, phase="PLAN", env={})
    assert estimate == dict(
        input_tokens=ceil((overhead + 700 * tokens_per_char) * 1.15),
        images=0,
        completion_tokens=1536,
    )


def test_completion_token_limits_use_phase_defaults_and_overrides(monkeypatch):
    assert completion_tokens_for("PLAN", {}) == 1536
    env = dict(CUTSIEVE_PLAN_MAX_TOKENS="2048", CUTSIEVE_COMPLETION_TOKENS="960")
    assert completion_tokens_for("PLAN", env) == 2048
    for phase in (None, "MATERIAL", "REVIEW"):
        assert completion_tokens_for(phase, {}) == 768
        assert completion_tokens_for(phase, env) == 960
    assert completion_tokens_for("PLAN", dict(env, CUTSIEVE_PLAN_MAX_TOKENS="100")) == 256
    assert completion_tokens_for("REVIEW", dict(env, CUTSIEVE_COMPLETION_TOKENS="0")) == 256
    assert completion_tokens_for("PLAN", dict(env, CUTSIEVE_PLAN_MAX_TOKENS="invalid")) == 1536
    monkeypatch.setenv("CUTSIEVE_PLAN_MAX_TOKENS", "2200")
    assert completion_tokens_for("PLAN") == 2200


def test_workflow_reserves_language_estimate_and_sends_the_fingerprinted_limit(
    source_store, model_env, visual_messages
):
    env = dict(model_env, CUTSIEVE_PLAN_MAX_TOKENS="1792", CUTSIEVE_COMPLETION_TOKENS="960")
    repo = WorkflowRepository(EditRepository(source_store))
    workspace = uid()
    project = source_store.create_project(workspace, "Budget")["project_id"]
    repo.create(workspace, dict(
        scope=dict(project_id=project), intent="EDIT", message="检查画面", idempotency_key=uid(),
        budget=dict(DEFAULT_BUDGET, max_cost_microunits=9000),
    ))
    claim = repo.claim(uid())
    text, _ = visual_messages[0]["content"]
    projected = [dict(role="user", content=[text, dict(type="image_url")])]
    expected_input = context_estimate(projected, [], env=env)["input_tokens"]
    assert context_estimate(visual_messages, [], phase="PLAN", env=env) == dict(
        input_tokens=expected_input, images=1, completion_tokens=1792,
    )

    response = dict(model="deepseek-chat", usage=dict(prompt_tokens=128, completion_tokens=17))
    transport = RecordingTransport([response, response])
    planner = FlashPlanner(env, transport)
    identity = planner.identity()

    def send():
        return planner.request(visual_messages, identity, [], 1)

    assert run_model(repo, claim, "plan", visual_messages, [], env, send, identity=identity) == response
    assert transport.payloads[0]["messages"] == visual_messages
    assert transport.payloads[0]["max_tokens"] == 1792
    (reservation,) = [
        event["payload"] for event in repo.page(workspace, claim.id, None, 20)["items"]
        if event["payload"].get("activity") == "model.reserved"
    ]
    expected_bound = expected_input + 1024
    assert reservation["estimated_input_tokens"] == expected_input
    assert reservation["reserved_input_token_bound"] == expected_bound
    assert reservation["completion_reserve_tokens"] == 1792
    assert reservation["context_estimator"] == "lang-aware-plus-image-allowance"
    with repo.sessions() as db:
        assert db.scalar(select(model_requests.c.reserved)) == expected_bound + 1792

    with pytest.raises(EditError) as conflict:
        changed = dict(env, CUTSIEVE_PLAN_MAX_TOKENS="1793")
        run_model(repo, claim, "plan", visual_messages, [], changed, send, identity=identity)
    assert conflict.value.code == "MODEL_INPUT_CONFLICT"
    assert len(transport.payloads) == 1
    planner.request([], identity, [], 1)
    assert transport.payloads[-1]["max_tokens"] == 960


@pytest.mark.parametrize("large_payload", [False, True])
def test_visual_window_caps_text_reservation(source_store, model_env, visual_messages, large_payload):
    catalog = MediaCatalog(source_store)
    workspace = uid()
    project = source_store.create_project(workspace, "Frames")["project_id"]
    upload = source_store.create_upload(
        workspace, project, dict(filename="source.mp4", size_bytes=1, content_type="video/mp4")
    )
    with catalog.write() as db:
        catalog.register(db, upload, dict(idempotency_key=uid(), requested_capabilities=["visual_observation"]))
    run = catalog.claim()
    payload_bytes = len(json.dumps(visual_messages).encode()) if large_payload else 200
    expected_text = 4096 if large_payload else 200
    response = dict(model="deepseek-chat")
    assert billed_window(catalog, run, "observe", payload_bytes, 1, model_env, lambda: response) == response
    usage = catalog.visual_usage(workspace, run.id)
    assert usage["uncertain_microunits"] == expected_text + 2048 + 768


def test_flash_reuses_a_loop_per_worker_and_checks_response_model(model_env):
    incompatible = [{}, {"model": ""}, {"model": "deepseek-reasoner"}, {"model": "deepseek-chat-mini"}]
    snapshot = dict(model="deepseek-chat-20260101")
    alias = dict(model="deepseek-chat")
    other_snapshot = dict(model="provider/deepseek-chat-2026-01-01")
    transport = RecordingTransport([snapshot, alias, *incompatible, other_snapshot])
    planner = FlashPlanner(model_env, transport)
    identity = planner.identity()
    assert planner.request([], identity, [], 1) == snapshot
    assert planner.request([], identity, [], 1, phase="PLAN") == alias
    assert [payload["max_tokens"] for payload in transport.payloads] == [768, 1536]
    for _ in incompatible:
        with pytest.raises(EditError) as error:
            planner.request([], identity, [], 1)
        assert error.value.code == "MODEL_UNAVAILABLE"

    loop = transport.loops[0]
    assert all(request_loop is loop for request_loop in transport.loops)
    assert not loop.is_closed()
    with ThreadPoolExecutor(max_workers=1) as worker:
        response = worker.submit(planner.request, [], identity, [], 1, max_tokens=1200).result(timeout=5)
    assert response == other_snapshot
    assert transport.payloads[-1]["max_tokens"] == 1200
    assert transport.loops[-1] is not loop


def test_render_document_uses_materialized_artifact_and_checks_metadata(tmp_path, make_checkpoint):
    cp = make_checkpoint()
    cp.attempt.revision_id = "revision"
    row = SimpleNamespace(workspace_id="workspace", checkpoint=cp)
    record = SimpleNamespace(kind="EXPORT", revision_id="revision", digest="0" * 64)
    path = tmp_path / "export.mp4"
    path.write_bytes(b"locally published output")
    worker = SimpleNamespace(artifact_record=lambda *_: record, materialize=lambda _: path)
    assert render_document(worker, row, "artifact", "EXPORT") == (record, path)

    for kind, revision in (("PREVIEW", "revision"), ("EXPORT", "other")):
        record.kind, record.revision_id = kind, revision
        with pytest.raises(EditError) as error:
            render_document(worker, row, "artifact", "EXPORT")
        assert error.value.code == "OUTPUT_REVISION_CHANGED"


@pytest.mark.parametrize(("times", "clip_ids"), [([2], ["c2"]), ([2, 2, 1, 0], ["c1", "c2"])])
def test_sampled_conflicts_attribute_every_clip_and_only_repair_one(times, clip_ids, make_checkpoint):
    first = dict(
        clip_id="c1", media_revision_id="media", source_start_us=0, source_end_us=2,
        evidence_ids=[], audio_policy="keep",
    )
    second = dict(first, clip_id="c2", source_start_us=3, source_end_us=4, evidence_ids=["old"])
    revision = dict(
        revision_id="revision", revision_no=1, clips=[first, second], output_profile={},
        caption_policy="preserve_source", total_duration_us=3,
    )
    manifest = dict(total_duration_us=3, clips=[
        dict(clip_id="c1", output_start_us=0, output_end_us=2),
        dict(clip_id="c2", output_start_us=2, output_end_us=3),
    ])
    cp = make_checkpoint(scope=dict(project_id="project", media_revision_ids=["media"]), duration_target_us=3)
    cp.attempt.revision_id = revision["revision_id"]
    cp.attempt.shot_plan = dict(shots=[dict(target_clip_id="c2", evidence_ids=["old"], candidate_id="initial")])
    cp.attempt.candidate_snapshot = [dict(
        candidate_id="new", media_revision_id="media", range=dict(start_us=5, end_us=6),
        support="SUPPORTED", evidence_ids=["new-evidence"], reason="Confirmed from source frames",
    )]
    run_id = uid()
    state = new_state(run_id, "workspace", "project", dict(DEFAULT_BUDGET, max_cost_microunits=100))
    row = SimpleNamespace(id=run_id, checkpoint=cp, state=state)
    semantic = dict(support="CONFLICTING", checked_times_us=times, sequence_id="sequence", reason="Wrong event")
    report = review_report(row, "PREVIEW", manifest=manifest, semantic=semantic)
    issue = review_issue(report, semantic, manifest)
    assert issue["code"] == "SAMPLED_CLIP_CONFLICT"
    assert issue["affected_clip_ids"] == clip_ids
    assert issue["evidence_refs"] == ["sequence"]
    repair = repair_for(row, issue, revision)
    if len(clip_ids) == 1:
        assert issue["repairability"] == "AUTO"
        assert repair.attempt.target_clip_ids == ["c2"]
        assert repair.attempt.preserved_clip_ids == ["c1"]
    else:
        assert issue["repairability"] == "MANUAL"
        assert repair is None
