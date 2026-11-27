"""Two runs race for the same workspace allowance without calling an external provider."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import time
from threading import Barrier

import pytest
from sqlalchemy import select, update
from dovideo.application.edit_instructions import PROFILE
from dovideo.application.edit_sources import EditSources
from dovideo.application.edit_timeline import EditError, uid
from dovideo.application.workflow_state import DEFAULT_BUDGET, NeedsInput
from dovideo.infrastructure.persistence.edit_repository import EditRepository
from dovideo.infrastructure.persistence.edit_tables import EDIT_TABLES, revisions
from dovideo.infrastructure.persistence.source_tables import projects, sources
from dovideo.infrastructure.persistence.sqlalchemy import Base, create_sqlalchemy_engine
from dovideo.infrastructure.persistence.workflow_budget import run_model
from dovideo.infrastructure.persistence.workflow_repository import StaleLease, WorkflowRepository
from dovideo.infrastructure.persistence.workflow_tables import WORKFLOW_TABLES, model_requests
from dovideo.infrastructure.persistence.workspace_budget_table import workspace_budgets


@pytest.fixture
def store(tmp_path):
    engine = create_sqlalchemy_engine(f"sqlite:///{tmp_path}/budget.db")
    Base.metadata.create_all(engine, tables=[projects, sources, *EDIT_TABLES, *WORKFLOW_TABLES])
    yield WorkflowRepository(EditRepository(EditSources(engine, tmp_path)))
    engine.dispose()


def test_nonwriting_stages_and_expired_claims_cannot_patch(store):
    repo = store
    workspace = uid()
    project = repo.sources.create_project(workspace, "Team")["project_id"]
    edit = repo.edits.create(
        workspace, dict(project_id=project, title="Review", output_profile=PROFILE, idempotency_key=uid())
    )["edit_project_id"]
    run = repo.create(
        workspace,
        dict(
            scope=dict(project_id=project),
            intent="EDIT",
            edit_project_id=edit,
            message="调整成片尺寸",
            idempotency_key=uid(),
        ),
    )["run_id"]

    def step(claim, next_phase):
        repo.advance(claim, state={"phase": next_phase})
        return repo.claim(uid())

    def propose(claim, width):
        patch = dict(
            expected_revision=0,
            reason="调整画宽",
            idempotency_key=uid(),
            operations=[dict(op="set_output_profile", output_profile=dict(PROFILE, width=width))],
        )
        return repo.edits.patch(
            workspace, edit, patch, guard=lambda db: repo.writer_guard(db, claim, "COMMIT")
        )

    editor = step(step(repo.claim(uid()), "MATERIAL"), "PROPOSE")
    coordinator = step(editor, "COMMIT")
    commit = propose(coordinator, 640)
    with pytest.raises(EditError, match="时间线已更新"):
        propose(coordinator, 1280)
    reviewer = step(step(coordinator, "RENDER"), "REVIEW")

    # An editor can finish a late request while the reviewer is active.
    # Both target the very same timeline with different changes.
    gate = Barrier(2)

    def send(claim, width):
        gate.wait(timeout=5)
        return propose(claim, width)

    with ThreadPoolExecutor(2) as pool:
        late = pool.submit(send, editor, 1280)
        current = pool.submit(send, reviewer, 320)
        with pytest.raises(StaleLease):
            late.result()
        with pytest.raises(EditError, match="没有时间线"):
            current.result()
    with repo.sessions() as db:
        heads = db.scalars(
            select(revisions.c.id).where(revisions.c.edit_id == edit).order_by(revisions.c.number)
        ).all()
    assert len(heads) == 2 and heads[-1] == commit["revision_id"]
    repo.control(workspace, run, "cancel", {"idempotency_key": uid()})


def test_two_runs_share_workspace_cap(store):
    repo = store
    workspace = uid()
    project = repo.sources.create_project(workspace, "Budget")["project_id"]
    claims = []
    for phases in (("MATERIAL", "PROPOSE"), ("MATERIAL", "PROPOSE", "COMMIT", "RENDER", "REVIEW")):
        run = repo.create(
            workspace,
            dict(
                scope=dict(project_id=project),
                intent="EDIT",
                message="检查画面",
                idempotency_key=uid(),
                budget=dict(DEFAULT_BUDGET, max_cost_microunits=100),
            ),
        )
        claim = repo.claim(uid())
        assert claim.id == run["run_id"]
        for phase in phases:
            repo.advance(claim, state={"phase": phase})
            claim = repo.claim(uid())
        claims.append(claim)
    gate = Barrier(2)
    limits = dict(
        DOVIDEO_WORKSPACE_CNY_MAX_COST_MICROUNITS="2",
        DOVIDEO_FLASH_PRICE_VERSION="fixture",
        DOVIDEO_FLASH_CNY_INPUT_MICROS_PER_MILLION="1000",
        DOVIDEO_FLASH_CNY_OUTPUT_MICROS_PER_MILLION="0",
        DOVIDEO_WORKFLOW_STAGE_MAX_MODEL_CALLS="1",
    )

    def spend(claim):
        gate.wait(timeout=5)
        try:
            return run_model(repo, claim, "model", [], [], limits, lambda: {"choices": []})
        except NeedsInput as exc:
            return exc.input["kind"]

    with ThreadPoolExecutor(2) as threads:
        outcomes = list(threads.map(spend, claims))
    assert outcomes.count("BUDGET") == 1 and outcomes.count({"choices": []}) == 1
    winner = claims[outcomes.index({"choices": []})]
    # A missing usage record stays uncertain. A known reply must not be sent twice.
    assert run_model(repo, winner, "model", [], [], limits, lambda: pytest.fail("duplicate request")) == {
        "choices": []
    }
    with pytest.raises(NeedsInput):
        run_model(
            repo,
            winner,
            "next",
            [],
            [],
            dict(limits, DOVIDEO_WORKSPACE_CNY_MAX_COST_MICROUNITS="20"),
            lambda: pytest.fail("exceeded stage budget"),
        )
    with repo.sessions() as db:
        assert (
            db.execute(
                select(workspace_budgets.c.exposure).where(workspace_budgets.c.workspace == workspace)
            ).scalar_one()
            == 2
        )
        origin = db.execute(
            select(model_requests.c.price).where(model_requests.c.run_id == winner.id)
        ).scalar_one()
        assert origin["budget_scope"] == winner.state["phase"]
    for claim in claims:
        repo.control(workspace, claim.id, "cancel", {"idempotency_key": uid()})


def test_fenced_patch_reads_scoped_workflow_proof_in_its_own_transaction(store):
    repo = store
    workspace = uid()
    project = repo.sources.create_project(workspace, "Evidence")["project_id"]
    asset = repo.sources.create_upload(
        workspace, project, dict(filename="fixture.mp4", size_bytes=16, content_type="video/mp4")
    )
    mid = asset.revision
    with repo.edits.write() as db:
        db.execute(
            update(sources)
            .where(sources.c.id == asset.id)
            .values(
                digest="0" * 64,
                receipt="a" * 64,
                probe=dict(
                    duration_us=1000000, width=320, height=240, streams=[], source_origin=dict(num=0, den=1)
                ),
            )
        )
    accepted = repo.create(
        workspace,
        dict(scope=dict(project_id=project), intent="EDIT", message="剪辑", idempotency_key=uid()),
    )
    claim = repo.claim(uid())
    assert claim.id == accepted["run_id"]
    evidence = dict(project_id=project, media_revision_id=mid, range=dict(start_us=0, end_us=500000))
    with repo.edits.write() as db:
        proof = repo.output(db, claim, "verified", evidence, "EVIDENCE")
    repo.advance(claim, state=dict(phase="COMMIT"))
    writer = repo.claim(uid())
    edit = repo.edits.create(
        workspace, dict(project_id=project, title="Evidence", output_profile=PROFILE, idempotency_key=uid())
    )

    def resolve(workspace, key, *, session):
        assert session.in_transaction()
        return repo.document(workspace, key, session=session).body

    repo.edits.resolve_evidence = resolve
    clip = dict(
        media_revision_id=mid,
        source_start_us=0,
        source_end_us=500000,
        audio_policy="keep",
        evidence_ids=[proof],
    )
    patch = dict(
        expected_revision=0,
        idempotency_key=uid(),
        reason="evidence",
        operations=[dict(op="append_clip", clip=clip)],
    )
    receipt = repo.edits.patch(
        workspace, edit["edit_project_id"], patch, guard=lambda db: repo.writer_guard(db, writer, "COMMIT")
    )
    assert receipt["revision_id"] != edit["head_revision_id"]
    repo.control(workspace, writer.id, "cancel", {"idempotency_key": uid()})


def workflow_crash_child():
    from dovideo.infrastructure.persistence.work_outbox import WorkOutbox

    engine = create_sqlalchemy_engine(os.environ["VM_FAULT_DB"])
    repository = WorkflowRepository(EditRepository(EditSources(engine, Path(os.environ["VM_FAULT_DIR"]))))
    mode = sys.argv[-1]
    if mode == "dispatcher":
        event = WorkOutbox(repository.edits).claim()
        assert event
        print(event[0], flush=True)
    else:
        claim = repository.claim(uid(), lease_seconds=2)
        assert claim.id == os.environ["VM_FAULT_RUN"]
        if mode == "plan":
            repository.advance(claim, state=dict(phase="MATERIAL"), docs={"brief": {"source": "movie"}})
        else:
            repository.invoke(
                claim, "material-read", "video.get_segment", {}, lambda: {"record": "retained"}
            )
    # Simulate a process loss; no application shutdown, lease release or final message.
    os._exit(77)


def test_workflow_restart_after_lost_notifications(store, tmp_path, monkeypatch):
    from dovideo.infrastructure.persistence.workflow_repository import StaleLease
    from dovideo.infrastructure.persistence.outbox_tables import outbox
    from dovideo.infrastructure.persistence.work_outbox import WorkOutbox
    from dovideo.infrastructure.persistence.workflow_tables import runs

    repo = store
    workspace = uid()
    project = repo.sources.create_project(workspace, "Recovery")["project_id"]
    state = repo.create(
        workspace,
        dict(scope=dict(project_id=project), intent="EDIT", message="恢复", idempotency_key=uid()),
    )
    rid = state["run_id"]
    engine = repo.sessions.kw["bind"]
    environment = dict(
        os.environ,
        VM_FAULT_DB=engine.url.render_as_string(hide_password=False),
        VM_FAULT_RUN=rid,
        VM_FAULT_DIR=str(tmp_path),
    )
    for component in ("plan", "dispatcher", "material"):
        exited = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), component], env=environment, timeout=30,
            capture_output=True, text=True,
        )
        assert exited.returncode == 77, exited.stderr
        if component == "dispatcher":
            event_id = exited.stdout.strip()
    with repo.edits.write() as db:
        old = db.execute(select(runs).where(runs.c.id == rid)).one()
        assert db.scalar(select(outbox.c.status).where(outbox.c.id == event_id)) == "LEASED"
        db.execute(update(outbox).where(outbox.c.id == event_id).values(lease=0))
    relay = WorkOutbox(repo.edits)
    while relay.dispatch_one():
        pass
    assert not relay.receive(event_id)
    with repo.sessions() as db:
        assert db.scalar(select(outbox.c.status).where(outbox.c.id == event_id)) == "SENT"
    # Moving the worker clock into the future must not transfer a live DB lease.
    actual = time.time
    monkeypatch.setattr(time, "time", lambda: actual() + 86400)
    repo.renew(old)
    assert repo.claim(uid()) is None
    # Expire the lease in SQL to avoid sleeping for a whole heartbeat period.
    with repo.edits.write() as db:
        db.execute(update(runs).where(runs.c.id == rid).values(lease=1))
    fresh = WorkflowRepository(repo.edits)
    current = fresh.claim(uid())
    assert current.id == rid and current.fence > old.fence
    assert fresh.document(workspace, current.checkpoint.attempt.brief_id).body == {"source": "movie"}
    assert fresh.invoke(
        current, "material-read", "video.get_segment", {}, lambda: pytest.fail("repeated side effect")
    ) == {"record": "retained"}
    with pytest.raises(StaleLease):
        repo.renew(old)
    fresh.advance(current, state=dict(phase="PROPOSE"))
    assert fresh.claim(uid()).state["phase"] == "PROPOSE"
    fresh.control(workspace, rid, "cancel", {"idempotency_key": uid()})


if __name__ == "__main__":
    workflow_crash_child()
