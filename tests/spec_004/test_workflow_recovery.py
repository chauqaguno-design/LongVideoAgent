"""Actual SQL contention, checkpoint recovery and receipt-first control operations."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy import create_engine, select, update

from cutsieve.application.edit_sources import EditSources
from cutsieve.application.edit_timeline import EditError, uid
from cutsieve.application.stages import StageDeps
from cutsieve.application.stages.edit import run_edit
from cutsieve.application.stages.plan import model_plan
from cutsieve.application.workflow_checkpoint import ModelIdentity, PlanAttempt
from cutsieve.infrastructure.persistence.edit_repository import EditRepository
from cutsieve.infrastructure.persistence.edit_tables import EDIT_TABLES
from cutsieve.infrastructure.persistence.source_tables import projects, sources
from cutsieve.infrastructure.persistence.sqlalchemy import Base
from cutsieve.infrastructure.persistence.workflow_repository import StaleLease, WorkflowRepository
from cutsieve.infrastructure.persistence.workflow_tables import WORKFLOW_TABLES, runs
from cutsieve.infrastructure.persistence.workflow_worker import WorkflowWorker


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


def ready_source(repo, workspace, project, duration_us):
    source = repo.sources.create_upload(
        workspace, project, dict(filename="source.mp4", size_bytes=16, content_type="video/mp4")
    )
    with repo.edits.write() as db:
        db.execute(update(sources).where(sources.c.id == source.id).values(
            digest="a" * 64, receipt="b" * 64,
            probe=dict(duration_us=duration_us, streams=[], source_origin=dict(num=0, den=1)),
        ))
    return source


def test_claim_lease_and_recovery(setup):
    repo, workspace, body = setup
    a = repo.create(workspace, body)
    assert repo.create(workspace, body) == a
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
    log = repo.page(workspace, old.id, None, 10)["items"]
    assert [e["sequence"] for e in log] == list(range(1, len(log) + 1))
    assert repo.event_cursor(workspace, old.id, log[0]["event_id"]) == 1
    assert repo.get(workspace, old.id)["budget_usage"]["tool_calls"] == 1


def test_invalid_reference_rolls_back_before_checkpoint_publication(setup):
    from pydantic import ValidationError
    from sqlalchemy.exc import StatementError

    repo, workspace, body = setup
    repo.create(workspace, body)
    claim = repo.claim(uid())
    with pytest.raises(StatementError) as error:
        repo.advance(claim, references={"incomplete": {"type": "EVIDENCE"}}, state=dict(phase="MATERIAL"))
    assert isinstance(error.value.orig, ValidationError)
    with repo.sessions() as db:
        row = repo.readable(db, workspace, claim.id)
    assert row.checkpoint.context.references == {} and row.state == claim.state


def test_input_identity_and_phase_budget_survive_new_plan_and_worker(setup, monkeypatch):
    from cutsieve.application.workflow_state import DEFAULT_BUDGET, NeedsInput
    from cutsieve.infrastructure.persistence.workflow_budget import run_model
    from cutsieve.infrastructure.persistence.workflow_tables import model_requests

    repo, workspace, body = setup
    monkeypatch.setenv("CUTSIEVE_WORKFLOW_STAGE_MAX_MODEL_CALLS", "1")
    repo.create(workspace, dict(body, budget=dict(DEFAULT_BUDGET, max_cost_microunits=1000)))
    env = dict(
        CUTSIEVE_WORKSPACE_CNY_MAX_COST_MICROUNITS="1000",
        CUTSIEVE_FLASH_PRICE_VERSION="fixture",
        CUTSIEVE_FLASH_CNY_INPUT_MICROS_PER_MILLION="25",
        CUTSIEVE_FLASH_CNY_OUTPUT_MICROS_PER_MILLION="25",
    )
    reply = dict(usage=dict(prompt_tokens=12, completion_tokens=3), choices=[])
    old = repo.claim(uid())
    messages = [dict(role="user", content="original")]
    identity = dict(model="fixture", fingerprint="deployment", mode="native")
    assert run_model(repo, old, "plan", messages, [], env, lambda: reply, identity=identity) == reply
    with repo.edits.write() as db:
        db.execute(update(runs).where(runs.c.id == old.id).values(lease=0))
    current = WorkflowRepository(repo.edits).claim(uid())
    assert current.fence > old.fence and current.checkpoint.attempt.plan_generation == 0
    no_call = lambda: pytest.fail("unexpected physical request")
    assert run_model(repo, current, "plan", messages, [], env, no_call, identity=identity) == reply
    for prompt, tools, model in (
        ([], [], identity),
        (messages, [{"name": "different"}], identity),
        (messages, [], dict(identity, mode="json")),
    ):
        with pytest.raises(EditError) as error:
            run_model(repo, current, "plan", prompt, tools, env, no_call, identity=model)
        assert error.value.code == "MODEL_INPUT_CONFLICT"
    with pytest.raises(IntegrityError), repo.edits.write() as db:
        db.execute(
            update(model_requests).where(model_requests.c.run_id == old.id).values(request_fingerprint=None)
        )
    assert run_model(repo, current, "plan", messages, [], env, no_call, identity=identity) == reply
    repo.advance(current, state=dict(status="AWAITING_INPUT", awaiting_input=NeedsInput("Which?").input))
    state = repo.get(workspace, current.id)
    repo.control(
        workspace,
        current.id,
        "resume",
        dict(
            expected_state_version=state["state_version"],
            message="Another goal",
            idempotency_key="clarify-plan",
        ),
    )
    current = repo.claim(uid())
    assert current.checkpoint.attempt.plan_generation == current.checkpoint.attempt.turn == 1
    with pytest.raises(NeedsInput) as error:
        run_model(repo, current, "plan", messages, [], env, no_call, identity=identity)
    assert error.value.input["kind"] == "BUDGET"
    assert repo.get(workspace, current.id)["budget_usage"]["model_calls"] == 1


def test_skills_are_lazy_scoped_and_pinned(setup, monkeypatch):
    import cutsieve.application.workflow_skills as skills

    repo, workspace, body = setup
    repo.create(workspace, body)
    first = repo.claim(uid())
    registry = skills.metadata()
    assert len(skills.catalog_summary()) == 5
    assert "content" not in skills.catalog_summary()[0]
    monkeypatch.setitem(
        registry["grounded-editing"],
        "tools",
        [
            *registry["grounded-editing"]["tools"],
            "edit.apply_patch",
            "render.submit",
        ],
    )
    allowed = {"edit.apply_patch", "render.submit", "video.search_evidence", "edit.get_timeline"}
    snapshot, tools, visible = skills.use_skill(
        repo, first, "grounded-editing", allowed
    )
    assert visible == {"video.search_evidence", "edit.get_timeline"}
    assert len(tools) == 2 and len(snapshot["content_hash"]) == 64
    # Pinning the skill writes through a different transaction than the stage's claim.
    repo.advance(first, attempt=PlanAttempt(model_round=1), state=dict(phase="PLAN"))
    later = WorkflowRepository(repo.edits).claim(uid())
    assert later.checkpoint.attempt.model_round == 1
    monkeypatch.setattr(skills, "metadata", lambda: pytest.fail("read mutable skill after recovery"))
    assert skills.use_skill(repo, later, "grounded-editing", allowed)[0] == snapshot

    with pytest.raises(EditError) as error:
        skills.use_skill(repo, later, "grounded-editing", allowed - {"edit.get_timeline"})
    assert error.value.code == "TOOL_SCHEMA_CHANGED"


def test_recovered_plan_persists_fingerprint_without_provider_reasoning(setup):
    from cutsieve.application.workflow_state import DEFAULT_BUDGET, NeedsInput
    from cutsieve.infrastructure.persistence.workflow_tables import model_requests

    repo, workspace, body = setup
    repo.create(workspace, dict(body, budget=dict(DEFAULT_BUDGET, max_cost_microunits=1000)))
    claim = repo.claim(uid())
    identity = dict(model="fixture", fingerprint="new-fingerprint", mode="native")
    pinned = ModelIdentity(**dict(identity, fingerprint="original-fingerprint"))
    repo.advance(claim, spec=claim.checkpoint.spec.pin_model(pinned))
    resumed = repo.claim(uid())

    def request(messages, provider_identity, tools, remaining):
        assert provider_identity == identity
        assert "Task skill guidance:" in messages[0]["content"]
        context = next(tool for tool in tools if tool["function"]["name"] == "video__get_context_window")
        assert "offset" in context["function"]["parameters"]["properties"]
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"type":"FINAL","plan":{"question":"Pick a source range"}}',
                        "reasoning_content": "provider internal",
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    adapter = SimpleNamespace(
        identity=lambda: identity,
        request=request,
        env=dict(
            CUTSIEVE_WORKSPACE_CNY_MAX_COST_MICROUNITS="1000",
            CUTSIEVE_FLASH_PRICE_VERSION="fixture",
            CUTSIEVE_FLASH_CNY_INPUT_MICROS_PER_MILLION="20",
            CUTSIEVE_FLASH_CNY_OUTPUT_MICROS_PER_MILLION="20",
        ),
    )
    with pytest.raises(NeedsInput, match="Pick a source range"):
        model_plan(StageDeps(repo, None, adapter), resumed, "scene")
    with repo.sessions() as db:
        receipt = db.execute(select(model_requests).where(model_requests.c.run_id == claim.id)).one()
    assert receipt.step == "plan-0-0:g0"
    assert (
        receipt.request_fingerprint and "reasoning_content" not in receipt.response["choices"][0]["message"]
    )
    assert repo.harness_view(workspace, claim.id, 0)["harness_version"] == 1


@pytest.mark.parametrize(
    ("kind", "code"),
    [("PREVIEW", "OUTPUT_REVISION_CHANGED"), ("EXPORT", "OUTPUT_DIGEST_CHANGED")],
)
def test_post_render_validation_persists_failed_report(setup, tmp_path, monkeypatch, kind, code):
    repo, workspace, body = setup
    repo.create(workspace, body)
    claim = repo.claim(uid())
    revision_id, job_id, artifact_id = uid(), uid(), uid()
    attempt = PlanAttempt(revision_id=revision_id)
    if kind == "PREVIEW":
        attempt.render_job_id = job_id
    else:
        attempt.export_job_id, attempt.export_artifact_id = job_id, artifact_id
    repo.advance(claim, state=dict(phase="REVIEW"), attempt=attempt)

    job = dict(status="SUCCEEDED", profile=kind, artifact_id=artifact_id)
    monkeypatch.setattr(repo.edits, "get", lambda *_: job)
    path = tmp_path / "output.mp4"
    path.write_bytes(b"rendered bytes")
    artifact = SimpleNamespace(
        kind=kind,
        revision_id=uid() if code == "OUTPUT_REVISION_CHANGED" else revision_id,
        digest="0" * 64,
    )
    renderer = SimpleNamespace(artifact_record=lambda *_: artifact, materialize=lambda _: path)
    worker = WorkflowWorker(repo, SimpleNamespace(state=SimpleNamespace(edit_worker=renderer)), None)
    worker.run_stage(repo.claim(worker.worker_id))

    state = repo.get(workspace, claim.id)
    assert state["status"] == "FAILED" and state["error"]["code"] == code
    (report_id,) = repo.harness_view(workspace, claim.id, 0)["report_ids"]
    report = repo.document(workspace, report_id).body
    assert report["report_id"] == report_id
    assert report["timeline_revision_id"] == revision_id and report["output_kind"] == kind
    assert report["output_artifact_id"] is None and report["output_sha256"] is None
    assert report["structural_checks"] == [{"code": "FFMPEG_VALIDATION", "status": "FAILED"}]
    assert report["semantic_checks"] == [{"code": "SAMPLED_CONTENT", "status": "NOT_CHECKED"}]
    (issue,) = report["issues"]
    assert issue["code"] == code and issue["repairability"] == "MANUAL"


def test_await_resume_budget_and_supersede(setup):
    repo, w, body = setup
    original = repo.create(w, body)
    worker = WorkflowWorker(repo, SimpleNamespace(state=SimpleNamespace()), None)
    worker.run_stage(repo.claim(uid()))
    waiting = repo.get(w, original["run_id"])
    assert waiting["status"] == "AWAITING_INPUT"
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
    from cutsieve.application.workflow_state import DEFAULT_BUDGET, NeedsInput
    from cutsieve.infrastructure.persistence.workflow_budget import run_model

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
        "CUTSIEVE_WORKSPACE_CNY_MAX_COST_MICROUNITS": "500000",
        "CUTSIEVE_FLASH_CNY_INPUT_MICROS_PER_MILLION": "200",
        "CUTSIEVE_FLASH_CNY_OUTPUT_MICROS_PER_MILLION": "1000",
        "CUTSIEVE_FLASH_PRICE_VERSION": "contract-fixture",
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
    from cutsieve.application.edit_instructions import PROFILE

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
    src = ready_source(repo, w, body["scope"]["project_id"], 900_000)
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
    from cutsieve.infrastructure.persistence.work_outbox import WorkOutbox, emit

    repo, workspace, body = setup
    run = repo.create(workspace, body)
    claim = repo.claim(uid())
    job = uid()
    repo.advance(
        claim,
        state=dict(phase="RENDER", status="WAITING_JOB", pending_job_ids=[job]),
        attempt=PlanAttempt(proposal_id=uid(), render_job_id=job),
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
        and woke.checkpoint.attempt.render_job_id == job
        and woke.state["phase"] == "RENDER"
    )
    with pytest.raises(StaleLease):
        repo.advance(claim, state={"status": "SUCCEEDED"})


def test_read_tools_refuse_resources_outside_frozen_run(setup):
    from cutsieve.application.workflow_tools import read_tool

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
    from cutsieve.application.workflow_state import DEFAULT_BUDGET
    from cutsieve.infrastructure.persistence.workflow_budget import run_model

    repo, workspace, body = setup
    run = repo.create(workspace, dict(body, budget=dict(DEFAULT_BUDGET, max_cost_microunits=1000)))
    claim = repo.claim(uid())
    started, release = Event(), Event()
    env = dict(
        CUTSIEVE_WORKSPACE_CNY_MAX_COST_MICROUNITS="1000",
        CUTSIEVE_FLASH_PRICE_VERSION="test",
        CUTSIEVE_FLASH_CNY_INPUT_MICROS_PER_MILLION="20",
        CUTSIEVE_FLASH_CNY_OUTPUT_MICROS_PER_MILLION="200",
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
    from cutsieve.application.workflow_state import DEFAULT_BUDGET

    repo, workspace, body = setup
    repo.create(workspace, dict(body, budget=dict(DEFAULT_BUDGET, max_cost_microunits=1000)))
    requests = []
    available_tools = []

    def request(dialogs, identity, tools, deadline):
        requests.append(dialogs)
        available_tools.append(tools)
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
            CUTSIEVE_WORKSPACE_CNY_MAX_COST_MICROUNITS="1000",
            CUTSIEVE_FLASH_PRICE_VERSION="test",
            CUTSIEVE_FLASH_CNY_INPUT_MICROS_PER_MILLION="100",
            CUTSIEVE_FLASH_CNY_OUTPUT_MICROS_PER_MILLION="100",
        ),
        identity=lambda: dict(model="fixture", mode="native", fingerprint="fixture"),
        request=request,
    )
    deps = StageDeps(repo, None, planner)
    claim = repo.claim(uid())
    for _ in range(3):
        repo.advance(claim, **model_plan(deps, claim, "剪车经过路口的部分"))
        claim = repo.claim(uid())
    paired = [m for m in requests[1] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in paired] == ["lookup", "write"]
    assert all(json.loads(m["content"]) == {"error": "TOOL_DENIED"} for m in paired)
    with pytest.raises(EditError) as err:
        model_plan(deps, claim, "剪车经过路口的部分")
    assert err.value.code == "MODEL_FORMAT" and len(requests) == 3
    assert available_tools[0] and available_tools[1] and not available_tools[2]


def test_run_deadline_and_bounded_recovery(setup):
    repo, w, body = setup
    run = repo.create(w, body)
    for _ in range(4):
        acquired = repo.claim(uid())
        assert acquired.id == run["run_id"]
        with repo.edits.write() as db:
            db.execute(update(runs).where(runs.c.id == acquired.id).values(lease=1))
    assert repo.claim(uid()) is None
    assert repo.get(w, run["run_id"])["error"]["code"] == "LEASE_RETRY_EXHAUSTED"
    late = repo.create(w, dict(body, idempotency_key=uid()))
    with repo.edits.write() as db:
        db.execute(update(runs).where(runs.c.id == late["run_id"]).values(deadline=1))
    assert repo.claim(uid()) is None and repo.get(w, late["run_id"])["error"]["code"] == "DEADLINE_EXCEEDED"


def test_steering_starts_a_fresh_plan_generation(setup):
    from cutsieve.application.workflow_state import NeedsInput

    repo, w, body = setup
    task = repo.create(w, body)
    coordinator = repo.claim(uid())
    previous = PlanAttempt(
        proposal_id=uid(), shot_plan_id=uid(), candidates_id=uid(), render_job_id=uid(),
        proposal=dict(base_revision=0, operations=[], brief_id=uid()),
        dialogs=[dict(role="assistant", content="earlier plan")],
    )
    repo.advance(coordinator, state=dict(phase="MATERIAL"), attempt=previous)
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
    assert replay.state["phase"] == "PLAN"
    attempt = replay.checkpoint.attempt
    assert attempt.turn == attempt.plan_generation == 1
    assert attempt.clarification == "剪出 1 到 2 秒"
    assert attempt.proposal is attempt.proposal_id is attempt.shot_plan_id is None
    assert attempt.candidates_id is attempt.render_job_id is None and attempt.dialogs == []
    assert replay.checkpoint.spec == coordinator.checkpoint.spec
    assert replay.checkpoint.repair.repair_count == 0
    with pytest.raises(StaleLease):
        repo.advance(member, state=dict(phase="COMMIT"))


def test_repair_reuses_source_snapshot_and_replays_committed_patch(setup):
    from cutsieve.application.edit_instructions import PROFILE, brief_for
    from cutsieve.application.workflow_checkpoint import EditBrief
    from cutsieve.application.workflow_review import repair_for
    from cutsieve.application.workflow_shots import candidate
    from cutsieve.application.workflow_state import DEFAULT_BUDGET, NeedsInput
    from cutsieve.infrastructure.persistence.edit_tables import revisions

    repo, workspace, body = setup
    project = body["scope"]["project_id"]
    source = ready_source(repo, workspace, project, 4_000_000)
    repo.edits.resolve_evidence = lambda ws, key, session=None: repo.document(ws, key, session=session).body
    repo.create(workspace, dict(body, budget=dict(DEFAULT_BUDGET, max_cost_microunits=1000)))
    row = repo.claim(uid())
    plan = dict(command="semantic", query="markers")
    brief = brief_for(row, plan, PROFILE)
    generation = uid()
    repo.advance(row, state=dict(phase="MATERIAL"), docs={"brief": brief}, attempt=PlanAttempt(plan=plan),
                 spec=row.checkpoint.spec.pin_index(generation).bind_brief(EditBrief(**brief)))

    row = repo.claim(uid())
    candidates = []
    with repo.edits.write() as db:
        for i in range(3):
            proof = dict(
                workspace_id=workspace, project_id=project, media_revision_id=source.revision,
                range=dict(start_us=i * 1_000_000, end_us=(i + 1) * 1_000_000),
                support="SUPPORTED", index_generation_id=generation, verification_protocol="ordered-pts-three-frame-v1",
            )
            key = repo.output(db, row, f"source-{i}", proof, "EVIDENCE")
            candidates.append(candidate(row, i, proof, dict(proof, evidence_id=key, reason="Marker confirmed")))
    chosen = [(c["media_revision_id"], c["range"], c["evidence_ids"]) for c in candidates[:2]]
    repo.advance(row, state=dict(phase="PROPOSE"), docs={"candidates": {"candidates": candidates}},
                 attempt=PlanAttempt(intervals=chosen, candidate_snapshot=candidates))
    deps = StageDeps(repo, None, None)
    for phase in ("PROPOSE", "COMMIT"):
        row = repo.claim(uid())
        assert row.state["phase"] == phase
        repo.advance(row, **run_edit(deps, row))
    row = repo.claim(uid())
    repo.advance(row, state=dict(phase="REVIEW"), attempt=PlanAttempt(preview_artifact_id=uid()))
    row = repo.claim(uid())
    old = row.checkpoint.attempt
    timeline = repo.edits.get(revisions, workspace, old.revision_id, lambda rev: rev.snapshot)
    kept, target = timeline["clips"]
    issue = dict(code="SAMPLED_CLIP_CONFLICT", affected_clip_ids=[target["clip_id"]])
    repair = repair_for(row, issue, timeline)
    assert repair is not None
    repo.advance(row, repair=repair, state=dict(phase="PROPOSE"), docs={"preview_review": {"issues": [issue]}})

    restarted = WorkflowRepository(repo.edits)
    deps = StageDeps(restarted, None, None)
    row = restarted.claim(uid())
    cp = row.checkpoint
    assert row.state["phase"] == "PROPOSE" and cp.attempt.turn == old.turn
    assert cp.attempt.plan_generation == cp.repair.repair_count == 1
    assert cp.repair.lineage[0]["shot_plan_id"] == old.shot_plan_id
    assert cp.attempt.shot_plan is cp.attempt.proposal is cp.attempt.revision_id is None
    assert cp.attempt.candidate_snapshot == candidates and cp.attempt.candidates_id == old.candidates_id
    # Even if an otherwise valid interval survives, a semantic edit needs a verified candidate.
    cp.attempt.candidate_snapshot = []
    with pytest.raises(NeedsInput):
        run_edit(deps, row)
    cp.attempt.candidate_snapshot = candidates
    proposal = run_edit(deps, row)
    shot_plan = proposal["docs"]["shot_plan"]
    assert shot_plan["candidate_snapshot_id"] == old.candidates_id and shot_plan["generation"] == 1
    assert shot_plan["shots"][0]["evidence_ids"] == candidates[2]["evidence_ids"]
    assert [s["id"] for s in shot_plan["skills"]] == ["timeline-refinement"]
    restarted.advance(row, **proposal)

    writer = restarted.claim(uid())
    committed = run_edit(deps, writer)  # Lose this worker after the timeline transaction.
    with restarted.edits.write() as db:
        db.execute(update(runs).where(runs.c.id == writer.id).values(lease=1))
    takeover = restarted.claim(uid())
    replay = run_edit(deps, takeover)
    assert takeover.fence > writer.fence and takeover.checkpoint.attempt.plan_generation == 1
    assert replay["attempt"].revision_id == committed["attempt"].revision_id
    restarted.advance(takeover, **replay)
    with restarted.sessions() as db:
        versions = db.scalars(select(revisions.c.snapshot).where(revisions.c.edit_id == old.edit_id)
                              .order_by(revisions.c.number)).all()
    assert len(versions) == 3
    first, second = versions[-1]["clips"]
    assert first == kept and second["clip_id"] == target["clip_id"]
    assert second["evidence_ids"] == candidates[2]["evidence_ids"]


def test_budget_resume_keeps_attempt_then_candidate_reply_starts_a_new_one(setup):
    from cutsieve.application.edit_instructions import PROFILE, brief_for
    from cutsieve.application.workflow_checkpoint import EditBrief
    from cutsieve.application.workflow_state import NeedsInput

    repo, workspace, body = setup
    source = ready_source(repo, workspace, body["scope"]["project_id"], 1_000_000)
    repo.create(workspace, body, conversation={"parent_run_id": None})
    claim = repo.claim(uid())
    selection = dict(media_revision_id=source.revision, range=dict(start_us=0, end_us=1_000_000),
                     support="UNCERTAIN", evidence_ids=[], index_generation_id=uid())
    pinned = claim.checkpoint.spec.pin_index(selection["index_generation_id"]).pin_model(
        ModelIdentity(model="fixture", fingerprint="deployment", mode="native")
    )
    brief = EditBrief(**brief_for(claim, {"command": "semantic"}, PROFILE))
    old = PlanAttempt(conversation_candidates=[selection], proposal_id=uid())
    repo.advance(claim, spec=pinned.bind_brief(brief), attempt=old, state=dict(
        phase="MATERIAL", status="AWAITING_INPUT", awaiting_input=NeedsInput("Budget?", kind="BUDGET").input,
    ))
    limits = dict(claim.state["budget_usage"]["limits"], deadline_seconds=1200)
    repo.control(workspace, claim.id, "budget", dict(reason="more time", new_limits=limits, idempotency_key=uid()))
    repo.control(workspace, claim.id, "resume", dict(message="继续", idempotency_key=uid()))
    row = repo.claim(uid())
    assert row.state["phase"] == "MATERIAL" and row.checkpoint.attempt.plan_generation == 0
    assert row.checkpoint.attempt.proposal_id == old.proposal_id and row.checkpoint.spec.brief == brief
    repo.advance(row, state=dict(status="AWAITING_INPUT", awaiting_input=NeedsInput("选哪一段？").input))
    repo.control(workspace, claim.id, "resume", dict(message="第1段", idempotency_key=uid()))
    row = repo.claim(uid())
    attempt = row.checkpoint.attempt
    assert row.state["phase"] == "PLAN" and attempt.turn == attempt.plan_generation == 1
    assert row.checkpoint.spec == pinned and row.checkpoint.repair.repair_count == 0
    assert attempt.selected_candidate == selection and attempt.conversation_candidates is None
    assert attempt.clarifications == [dict(question="选哪一段？", reply="第1段")]
    assert attempt.proposal_id is None
