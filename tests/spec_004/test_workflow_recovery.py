"""SQLite lease fencing, atomic edits and render wakeup recovery."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text, update

from cutsieve.application.agent_harness import AgentHarness
from cutsieve.application.edit_sources import EditSources
from cutsieve.application.edit_timeline import uid
from cutsieve.application.workflow_checkpoint import PendingRender
from cutsieve.infrastructure.persistence.edit_repository import EditRepository
from cutsieve.infrastructure.persistence.edit_tables import EDIT_TABLES, projects as edit_projects, revisions
from cutsieve.infrastructure.persistence.source_tables import projects, sources
from cutsieve.infrastructure.persistence.sqlalchemy import Base
from cutsieve.infrastructure.persistence.work_outbox import WorkOutbox, emit
from cutsieve.infrastructure.persistence.workflow_clock import db_now
from cutsieve.infrastructure.persistence.workflow_repository import StaleLease, WorkflowRepository
from cutsieve.infrastructure.persistence.workflow_tables import WORKFLOW_TABLES, runs


@pytest.fixture
def setup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/workflow.db")
    Base.metadata.create_all(engine, tables=[projects, sources, *EDIT_TABLES, *WORKFLOW_TABLES])
    store = EditSources(engine, tmp_path)
    repo = WorkflowRepository(EditRepository(store))
    workspace = uid()
    project = store.create_project(workspace, "Movie")["project_id"]
    yield repo, workspace, dict(scope=dict(project_id=project), message="剪出 1 到 2 秒", idempotency_key="workflow-0001")
    engine.dispose()


def ready_source(repo, workspace, project):
    source = repo.sources.create_upload(
        workspace, project, dict(filename="source.mp4", size_bytes=16, content_type="video/mp4")
    )
    with repo.edits.write() as db:
        db.execute(update(sources).where(sources.c.id == source.id).values(
            digest="a" * 64, receipt="b" * 64,
            probe=dict(duration_us=3_000_000, streams=[], source_origin=dict(num=0, den=1)),
        ))
    return source


def test_claim_lease_and_recovery(setup):
    repo, workspace, body = setup
    accepted = repo.create(workspace, body)
    assert repo.create(workspace, body) == accepted
    with ThreadPoolExecutor(2) as pool:
        winners = [row for row in pool.map(lambda _: repo.claim(uid()), range(2)) if row]
    assert len(winners) == 1
    old = winners[0]
    with repo.change(old) as (db, row):
        row.checkpoint.messages.append(dict(role="user", content=body["message"]))
        row.checkpoint.usage.model_calls = 1
        repo.save(db, row)
    with repo.edits.write() as db:
        db.execute(update(runs).where(runs.c.id == old.id).values(lease=1))
    restarted = WorkflowRepository(repo.edits)
    takeover = restarted.claim(uid())
    assert takeover.fence == old.fence + 1
    assert takeover.checkpoint.messages == [dict(role="user", content=body["message"])]
    with pytest.raises(StaleLease), repo.change(old):
        pytest.fail("Expired worker acquired the write transaction")
    with restarted.change(takeover) as (db, row):
        restarted.complete(db, row, row.checkpoint.result("Ready."))
    log = repo.page(workspace, old.id, None, 10)["items"]
    assert [event["sequence"] for event in log] == list(range(1, len(log) + 1))
    assert repo.event_cursor(workspace, old.id, log[0]["event_id"]) == 1
    assert repo.get(workspace, old.id)["usage"]["model_calls"] == 1


def test_process_loss_before_edit_checkpoint_does_not_leave_a_partial_edit(setup, fake_agent, monkeypatch):
    repo, workspace, body = setup
    ready_source(repo, workspace, body["scope"]["project_id"])
    repo.create(workspace, body)
    claim = repo.claim(uid())
    call = fake_agent.tool("edit", "edit_timeline", dict(clips=[dict(start_s=1, end_s=2)]))
    with repo.change(claim) as (db, row):
        row.checkpoint.messages.append(fake_agent.reply(call))
        repo.save(db, row)
    harness = AgentHarness(repo, None, None, transport=fake_agent)
    patch = repo.edits.patch

    class ProcessLost(BaseException):
        pass

    def crash_after_patch(*args, **kwargs):
        patch(*args, **kwargs)
        raise ProcessLost()

    with monkeypatch.context() as override:
        override.setattr(repo.edits, "patch", crash_after_patch)
        with pytest.raises(ProcessLost):
            harness.execute_tool(claim, call)
    with repo.edits.write() as db:
        assert db.execute(select(edit_projects)).first() is None
        assert db.execute(select(revisions)).first() is None
        db.execute(update(runs).where(runs.c.id == claim.id).values(lease=1))
    restarted = WorkflowRepository(repo.edits)
    new_claim = restarted.claim(uid())
    assert new_claim.checkpoint.tool_steps[0].status == "RUNNING"
    assert AgentHarness(restarted, None, None).execute_tool(new_claim, call) is False
    with restarted.sessions() as db:
        row = restarted.readable(db, workspace, new_claim.id)
        assert row.checkpoint.usage.tool_calls == 1
        assert row.checkpoint.tool_steps[0].ok is True
        assert len(db.execute(select(edit_projects)).all()) == 1
        assert sorted(db.scalars(select(revisions.c.number)).all()) == [0, 1]
    events = restarted.page(workspace, claim.id, None, 20)["items"]
    assert [e["type"] for e in events].count("TIMELINE_UPDATED") == 1


def test_active_workflow_fenced_on_source_tombstone(setup):
    repo, workspace, body = setup
    source = ready_source(repo, workspace, body["scope"]["project_id"])
    run = repo.create(workspace, body)
    old = repo.claim(uid())
    with repo.edits.write() as db:
        db.execute(update(sources).where(sources.c.id == source.id).values(probe={"deleted": True}))
    with pytest.raises(StaleLease), repo.change(old):
        pytest.fail("Deleted source still enabled publication")
    with pytest.raises(HTTPException):
        repo.get(workspace, run["run_id"])
    repo.stopped(old)
    with repo.sessions() as db:
        state = db.scalar(select(runs.c.state).where(runs.c.id == old.id))
        assert state["error"]["code"] == "SOURCE_REVOKED" and state["result_artifact_id"] is None


def test_waiting_checkpoint_wakeup_survives_worker_restart(setup):
    repo, workspace, body = setup
    run = repo.create(workspace, body)
    claim = repo.claim(uid())
    job, unrelated_event, event = uid(), uid(), uid()
    with repo.change(claim) as (db, row):
        row.checkpoint.pending_render_tool_call = PendingRender(
            tool_call_id="render", render_job_id=job, profile="PREVIEW", edit_project_id=uid(), revision_id=uid(),
        )
        row.state.update(status="WAITING_JOB", pending_job_ids=[job])
        repo.save(db, row, release=True, delay=60)
        emit(db, unrelated_event, workspace, uid(), "render.finished")
        emit(db, event, workspace, job, "render.finished")
    restored = WorkflowRepository(repo.edits)
    inbox = WorkOutbox(restored.edits)
    assert inbox.receive(unrelated_event) and restored.claim(uid()) is None
    assert inbox.receive(event) and not inbox.receive(event)
    woke = restored.claim(uid())
    assert woke.id == run["run_id"] and woke.checkpoint.pending_render_tool_call.render_job_id == job
    with pytest.raises(StaleLease), repo.change(claim):
        pytest.fail("Previous worker regained the waiting run")


def test_expired_deadline_and_bounded_lease_recovery(setup):
    repo, workspace, body = setup
    first = repo.create(workspace, body)["run_id"]
    with repo.edits.write() as db:
        db.execute(update(runs).where(runs.c.id == first).values(deadline=db_now(db) - 1))
    assert repo.claim(uid()) is None
    assert repo.get(workspace, first)["error"]["code"] == "DEADLINE_EXCEEDED"
    second = repo.create(workspace, dict(body, idempotency_key="workflow-0002"))["run_id"]
    for _ in range(4):
        assert repo.claim(uid()).id == second
        with repo.edits.write() as db:
            db.execute(update(runs).where(runs.c.id == second).values(lease=1))
    assert repo.claim(uid()) is None
    assert repo.get(workspace, second)["error"]["code"] == "LEASE_RETRY_EXHAUSTED"


def test_legacy_checkpoint_json_still_reads_original_scope_and_edit_ids(setup):
    repo, workspace, body = setup
    run_id = repo.create(workspace, body)["run_id"]
    edit, revision = uid(), uid()
    previous = dict(
        spec=dict(scope=dict(body["scope"], media_revision_ids=[]), message=body["message"], created=0),
        attempt=dict(edit_id=edit, revision_id=revision), context={}, repair={},
    )
    with repo.edits.write() as db:
        db.execute(text("UPDATE vm_workflow_runs SET checkpoint=:checkpoint WHERE id=:run_id"),
                   dict(run_id=run_id, checkpoint=json.dumps(previous)))
    with repo.sessions() as db:
        cp = repo.readable(db, workspace, run_id).checkpoint
        assert cp.spec.scope.project_id == body["scope"]["project_id"]
        assert cp.edit_project_id == edit and cp.revision_id == revision
