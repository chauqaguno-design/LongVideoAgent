"""Actual SQL contention, checkpoint recovery and receipt-first control operations."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, update

from dovideo.application.edit_sources import EditSources
from dovideo.application.edit_timeline import EditError, uid
from dovideo.application.spec_contracts import validate_contract
from dovideo.infrastructure.persistence.edit_repository import EditRepository
from dovideo.infrastructure.persistence.edit_tables import EDIT_TABLES
from dovideo.infrastructure.persistence.source_tables import projects, sources
from dovideo.infrastructure.persistence.sqlalchemy import Base
from dovideo.infrastructure.persistence.workflow_repository import StaleLease, WorkflowRepository
from dovideo.infrastructure.persistence.workflow_tables import WORKFLOW_TABLES, events, runs, tasks
from dovideo.infrastructure.persistence.workflow_worker import WorkflowWorker


@pytest.fixture
def setup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/workflow.db")
    Base.metadata.create_all(engine, tables=[projects, sources, *EDIT_TABLES, *WORKFLOW_TABLES])
    store = EditSources(engine, tmp_path)
    edits = EditRepository(store)
    repo = WorkflowRepository(edits)
    workspace = uid()
    project = store.create_project(workspace, "Movie")["project_id"]
    yield repo, workspace, dict(
        scope=dict(project_id=project),
        intent="EDIT",
        message="剪出 1 到 2 秒",
        idempotency_key="workflow-0001",
    )
    engine.dispose()


def test_claim_lease_and_recovery(setup):
    repo, workspace, body = setup
    a = repo.create(workspace, body, team=True)
    assert repo.create(workspace, body, team=True) == a
    with ThreadPoolExecutor(2) as pool:
        winners = [r for r in pool.map(lambda _: repo.claim(uid()), range(2)) if r]
    assert len(winners) == 1
    old = winners[0]
    assert repo.invoke(old, "first", "video.check_coverage", {"n": 1}, lambda: {"ran": True}) == {"ran": True}
    with repo.edits.write() as db:
        db.execute(update(runs).where(runs.c.id == old.id).values(lease=0))
    restarted = WorkflowRepository(repo.edits)
    takeover = restarted.claim(uid())
    assert takeover.fence == old.fence + 1
    assert restarted.invoke(
        takeover, "first", "video.check_coverage", {"n": 1}, lambda: pytest.fail("re-executed")
    ) == {"ran": True}
    with pytest.raises(StaleLease):
        repo.advance(old, state=dict(status="SUCCEEDED"))
    restarted.advance(takeover, state=dict(phase="MATERIAL"))
    for item in restarted.page(workspace, old.id, tasks, None, 10)["items"]:
        validate_contract("TeamTask", item)
    log = repo.page(workspace, old.id, events, None, 10)["items"]
    assert [e["sequence"] for e in log] == list(range(1, len(log) + 1))
    for e in log:
        validate_contract("Event", e)
    assert repo.event_cursor(workspace, old.id, log[0]["event_id"]) == 1
    assert repo.get(workspace, old.id)["budget_usage"]["tool_calls"] == 1


def test_await_resume_budget_and_supersede(setup):
    repo, w, body = setup
    original = repo.create(w, body)
    worker = WorkflowWorker(repo, SimpleNamespace(state=SimpleNamespace()), None)
    worker.run_stage(repo.claim(uid()))
    waiting = repo.get(w, original["run_id"])
    assert waiting["status"] == "AWAITING_INPUT"
    validate_contract("WorkflowRun", waiting)
    budget = dict(
        expected_state_version=waiting["state_version"],
        reason="More time",
        new_limits=dict(waiting["budget_usage"]["limits"], deadline_seconds=1200),
        idempotency_key="budget-adjust",
    )
    adjusted = repo.control(w, waiting["run_id"], "budget", budget)
    assert repo.control(w, waiting["run_id"], "budget", budget) == adjusted
    resume = dict(
        expected_state_version=adjusted["state_version"], message="开始", idempotency_key="resume-click"
    )
    resumed = repo.control(w, waiting["run_id"], "resume", resume)
    assert repo.control(w, waiting["run_id"], "resume", resume) == resumed
    with pytest.raises(EditError, match="幂等"):
        repo.control(w, waiting["run_id"], "resume", dict(resume, message="something different"))
    parallel = repo.create(w, dict(body, idempotency_key="independent-task"))
    replacement = repo.create(
        w,
        dict(
            body,
            idempotency_key="replacement-task",
            supersedes=dict(run_id=resumed["run_id"], expected_state_version=resumed["state_version"]),
        ),
    )
    assert repo.get(w, original["run_id"])["superseded_by_run_id"] == replacement["run_id"]
    assert repo.get(w, parallel["run_id"])["status"] == "QUEUED"
    with pytest.raises(EditError):
        repo.event_cursor(w, original["run_id"], parallel["last_event_id"])
    with pytest.raises(EditError):
        repo.get(uid(), parallel["run_id"])
    cancel = repo.control(w, parallel["run_id"], "cancel", dict(idempotency_key="stop-button"))
    assert cancel["status"] == "CANCELLED"
    with pytest.raises(EditError):
        repo.control(
            w, parallel["run_id"], "resume", dict(resume, expected_state_version=cancel["state_version"])
        )


def test_paid_attempts_bound_replay_uncertainty_and_images(setup):
    from dovideo.application.workflow_state import DEFAULT_BUDGET, NeedsInput
    from dovideo.infrastructure.persistence.workflow_budget import run_model

    repo, w, body = setup
    result = repo.create(
        w,
        dict(body, budget=dict(DEFAULT_BUDGET, max_cost_microunits=500_000, max_model_calls=2, max_images=1)),
    )
    claim = repo.claim(uid())
    image = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}],
        }
    ]
    env = {
        "DOVIDEO_WORKSPACE_CNY_MAX_COST_MICROUNITS": "500000",
        "DOVIDEO_FLASH_CNY_INPUT_MICROS_PER_MILLION": "200",
        "DOVIDEO_FLASH_CNY_OUTPUT_MICROS_PER_MILLION": "1000",
        "DOVIDEO_FLASH_PRICE_VERSION": "contract-fixture",
    }
    with pytest.raises(NeedsInput):
        run_model(repo, claim, "one", image, [], {}, lambda: pytest.fail("unpriced"))
    reply = {
        "usage": {"prompt_tokens": 30, "completion_tokens": 12},
        "choices": [{"message": {"content": "done"}}],
    }
    assert run_model(repo, claim, "one", image, [], env, lambda: reply) == reply
    assert run_model(repo, claim, "one", image, [], env, lambda: pytest.fail("paid twice")) == reply
    with pytest.raises(NeedsInput):
        run_model(repo, claim, "another-image", image, [], env, lambda: pytest.fail("image budget"))
    run_model(
        repo, claim, "two", [], [], env, lambda: {"choices": [{"message": {"content": "unknown usage"}}]}
    )
    with pytest.raises(NeedsInput):
        run_model(repo, claim, "third", [], [], env, lambda: pytest.fail("over budget"))
    state = repo.get(w, result["run_id"])
    usage = state["budget_usage"]
    assert usage["model_calls"] == 2 and usage["images"] == 1
    assert usage["input_tokens"] == 30 and usage["uncertain_microunits"] > 0
    assert usage["reserved_microunits"] == 0 and usage["committed_microunits"] > 0


def test_effect_commit_before_ledger_replays_business_receipt(setup):
    from dovideo.application.edit_instructions import PROFILE

    repo, workspace, body = setup
    run = repo.create(workspace, body)
    claim = repo.claim(uid())
    args = dict(
        project_id=run["project_id"], output_profile=PROFILE, title="Stable", idempotency_key=run["run_id"]
    )
    seen = []

    def disconnect():
        seen.append(repo.edits.create(workspace, args)["edit_project_id"])
        raise ConnectionError("lost after commit")

    with pytest.raises(ConnectionError):
        repo.invoke(claim, "edit-create", "edit.create", args, disconnect)
    receipt = WorkflowRepository(repo.edits).invoke(
        claim, "edit-create", "edit.create", args, lambda: repo.edits.create(workspace, args)
    )
    assert receipt["edit_project_id"] == seen[0]


def test_active_workflow_fenced_on_source_tombstone(setup):
    from fastapi import HTTPException

    repo, w, body = setup
    src = repo.sources.create_upload(
        w, body["scope"]["project_id"], dict(filename="source.mp4", size_bytes=16, content_type="video/mp4")
    )
    with repo.edits.write() as db:
        db.execute(
            update(sources)
            .where(sources.c.id == src.id)
            .values(
                probe={"duration_us": 900000, "streams": [], "source_origin": {"num": 0, "den": 1}},
                digest="0" * 64,
                receipt="1" * 64,
            )
        )
    run = repo.create(w, body)
    old = repo.claim(uid())
    with repo.edits.write() as db:
        db.execute(update(sources).where(sources.c.id == src.id).values(probe={"deleted": True}))
    with pytest.raises(StaleLease):
        repo.advance(old, state=dict(status="SUCCEEDED"), docs={"result": {"answer": "stale"}})
    with pytest.raises(HTTPException):
        repo.get(w, run["run_id"])
    with repo.edits.write() as db:
        db.execute(update(runs).where(runs.c.id == old.id).values(lease=0))
    assert repo.claim(uid()) is None
    with repo.sessions() as db:
        state = db.execute(select(runs.c.state).where(runs.c.id == old.id)).scalar_one()
        assert state["error"]["code"] == "SOURCE_REVOKED" and state["result_artifact_id"] is None


def test_waiting_checkpoint_wakeup_survives_worker_restart(setup):
    import time
    from dovideo.infrastructure.persistence.work_outbox import WorkOutbox, emit

    repo, workspace, body = setup
    run = repo.create(workspace, body)
    claim = repo.claim(uid())
    job = uid()
    repo.advance(
        claim,
        state=dict(phase="RENDER", status="WAITING_JOB", pending_job_ids=[job]),
        checkpoint={"proposal_id": uid(), "render_job_id": job},
    )
    with repo.edits.write() as db:
        db.execute(update(runs).where(runs.c.id == claim.id).values(ready_at=int(time.time()) + 60))
        old_event, event = uid(), uid()
        emit(db, old_event, workspace, uid(), "render.finished")
        emit(db, event, workspace, job, "render.finished")
    restored = WorkflowRepository(repo.edits)
    inbox = WorkOutbox(restored.edits)
    assert inbox.receive(old_event) and restored.claim(uid()) is None
    assert inbox.receive(event) and not inbox.receive(event)
    woke = restored.claim(uid())
    assert (
        woke.id == run["run_id"]
        and woke.checkpoint["render_job_id"] == job
        and woke.state["phase"] == "RENDER"
    )
    with pytest.raises(StaleLease):
        repo.advance(claim, state={"status": "SUCCEEDED"})


def test_read_tools_refuse_resources_outside_frozen_run(setup):
    from dovideo.application.workflow_tools import read_tool

    repo, workspace, body = setup
    repo.create(workspace, body)
    claim = repo.claim(uid())
    foreign = uid()

    def test_scope(tool, args):
        with pytest.raises(EditError) as exc:
            read_tool(repo, None, claim, tool, args)
        assert exc.value.status == 404

    test_scope("jobs.get", dict(job_id=foreign))
    test_scope("edit.get_timeline", dict(edit_project_id=foreign, revision_id=foreign))
    test_scope("video.get_segment", dict(media_revision_id=foreign, range=dict(start_us=0, end_us=10)))
    test_scope("video.search_evidence", dict(scope=dict(project_id=foreign), text="dialogue"))


def test_cancel_in_flight_settles_charge_without_accepting_result(setup):
    from threading import Event
    from dovideo.application.workflow_state import DEFAULT_BUDGET
    from dovideo.infrastructure.persistence.workflow_budget import run_model

    repo, workspace, body = setup
    run = repo.create(workspace, dict(body, budget=dict(DEFAULT_BUDGET, max_cost_microunits=1000)))
    claim = repo.claim(uid())
    started, release = Event(), Event()
    env = dict(
        DOVIDEO_WORKSPACE_CNY_MAX_COST_MICROUNITS="1000",
        DOVIDEO_FLASH_PRICE_VERSION="test",
        DOVIDEO_FLASH_CNY_INPUT_MICROS_PER_MILLION="20",
        DOVIDEO_FLASH_CNY_OUTPUT_MICROS_PER_MILLION="200",
    )

    def provider():
        started.set()
        assert release.wait(8)
        return {"usage": {"prompt_tokens": 20, "completion_tokens": 10}, "choices": []}

    with ThreadPoolExecutor(1) as pool:
        request = pool.submit(run_model, repo, claim, "inflight", [], [], env, provider)
        assert started.wait(8)
        accepted = repo.control(workspace, run["run_id"], "cancel", dict(idempotency_key=uid()))
        assert accepted["status"] == "RUNNING" and accepted["cancel_requested_at"]
        release.set()
        request.result(timeout=8)
    with pytest.raises(StaleLease):
        repo.advance(claim, state={"status": "SUCCEEDED"}, docs={"result": {"old": True}})
    with pytest.raises(StaleLease):
        run_model(
            repo, claim, "after-stop", [], [], env, lambda: pytest.fail("cancelled task issued new request")
        )
    state = repo.get(workspace, run["run_id"])
    assert state["status"] == "CANCELLED" and state["result_artifact_id"] is None
    assert state["budget_usage"]["committed_microunits"] == 2


def test_native_turn_records_each_denial_and_repairs_only_once(setup):
    import json
    from dovideo.application.workflow_state import DEFAULT_BUDGET
    from dovideo.application.workflow_steps import WorkflowSteps

    repo, workspace, body = setup
    repo.create(workspace, dict(body, budget=dict(DEFAULT_BUDGET, max_cost_microunits=1000)))
    requests = []

    def request(dialogs, identity, tools, deadline):
        requests.append(dialogs)
        calls = [
            dict(
                id="lookup",
                type="function",
                function=dict(name="jobs__get", arguments=json.dumps({"job_id": uid()})),
            ),
            dict(id="write", type="function", function=dict(name="edit__apply_patch", arguments="{}")),
        ]
        # The second and third turns try to reuse the first provider IDs.
        return {
            "choices": [{"message": dict(role="assistant", tool_calls=calls)}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 8},
        }

    planner = SimpleNamespace(
        env=dict(
            DOVIDEO_WORKSPACE_CNY_MAX_COST_MICROUNITS="1000",
            DOVIDEO_FLASH_PRICE_VERSION="test",
            DOVIDEO_FLASH_CNY_INPUT_MICROS_PER_MILLION="100",
            DOVIDEO_FLASH_CNY_OUTPUT_MICROS_PER_MILLION="100",
        ),
        identity=lambda: dict(model="fixture", mode="native", fingerprint="fixture"),
        request=request,
    )
    steps = WorkflowSteps(repo, None, planner)
    claim = repo.claim(uid())
    for _ in range(3):
        repo.advance(claim, **steps.model_plan(claim, "剪车经过路口的部分"))
        claim = repo.claim(uid())
    paired = [m for m in requests[1] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in paired] == ["lookup", "write"]
    assert all(json.loads(m["content"]) == {"error": "TOOL_DENIED"} for m in paired)
    with pytest.raises(EditError) as err:
        steps.model_plan(claim, "剪车经过路口的部分")
    assert err.value.code == "MODEL_FORMAT" and len(requests) == 3


def test_team_deadline_and_bounded_recovery(setup):
    repo, w, body = setup
    run = repo.create(w, body, team=True)
    for _ in range(4):
        acquired = repo.claim(uid())
        assert acquired.id == run["run_id"]
        with repo.edits.write() as db:
            db.execute(update(runs).where(runs.c.id == acquired.id).values(lease=1))
    assert repo.claim(uid()) is None
    assert repo.get(w, run["run_id"])["error"]["code"] == "LEASE_RETRY_EXHAUSTED"
    late = repo.create(w, dict(body, idempotency_key=uid()), team=True)
    with repo.edits.write() as db:
        db.execute(update(runs).where(runs.c.id == late["run_id"]).values(deadline=1))
    assert repo.claim(uid()) is None and repo.get(w, late["run_id"])["error"]["code"] == "DEADLINE_EXCEEDED"


def test_team_steering_appends_graph_without_reusing_old_cards(setup):
    from dovideo.application.workflow_state import NeedsInput

    repo, w, body = setup
    task = repo.create(w, body, team=True)
    coordinator = repo.claim(uid())
    repo.advance(coordinator, state=dict(phase="MATERIAL"))
    member = repo.claim(uid())
    repo.advance(member, state=dict(status="AWAITING_INPUT", awaiting_input=NeedsInput("请确认选段").input))
    waiting = repo.get(w, task["run_id"])
    repo.control(
        w,
        task["run_id"],
        "resume",
        dict(
            expected_state_version=waiting["state_version"], message="剪出 1 到 2 秒", idempotency_key=uid()
        ),
    )
    replay = repo.claim(uid())
    cards = repo.page(w, task["run_id"], tasks, None, 20)["items"]
    assert len({c["task_id"] for c in cards}) == 12
    assert cards[0]["status"] == "SUCCEEDED" and all(c["status"] == "CANCELLED" for c in cards[1:6])
    assert cards[6]["status"] == "RUNNING" and replay.checkpoint["turn"] == 1
    assert all(set(c["depends_on"]).issubset({other["task_id"] for other in cards[6:]}) for c in cards[6:])
