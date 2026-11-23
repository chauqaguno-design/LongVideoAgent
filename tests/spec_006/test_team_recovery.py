"""A fixed task graph is durable even when multiple workers can acquire it."""

from sqlalchemy import create_engine, select, update
import pytest
from copy import deepcopy
from dovideo.infrastructure.persistence.workflow_team import board, install_board

from dovideo.application.edit_sources import EditSources
from dovideo.application.edit_timeline import EditError, uid
from dovideo.application.spec_contracts import validate_contract
from dovideo.infrastructure.persistence.edit_repository import EditRepository
from dovideo.infrastructure.persistence.edit_tables import EDIT_TABLES
from dovideo.infrastructure.persistence.source_tables import projects, sources
from dovideo.infrastructure.persistence.sqlalchemy import Base
from dovideo.infrastructure.persistence.workflow_repository import WorkflowRepository, StaleLease
from dovideo.infrastructure.persistence.workflow_tables import (
    WORKFLOW_TABLES,
    attempts,
    deliveries,
    mail,
    runs,
    tasks,
)


def test_task_mailbox_fencing_cancel(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/team.db")
    Base.metadata.create_all(engine, tables=[projects, sources, *EDIT_TABLES, *WORKFLOW_TABLES])
    store = EditSources(engine, tmp_path)
    scope = uid()
    project = store.create_project(scope, "Team")["project_id"]
    repo = WorkflowRepository(EditRepository(store))
    graph_run = uid()
    baseline = board(graph_run, scope, "EDIT")
    for invalid in ("cycle", "foreign", "missing", "unknown_role"):
        graph = deepcopy(baseline)
        if invalid == "cycle":
            graph[0]["body"]["depends_on"] = [graph[-1]["id"]]
        if invalid == "foreign":
            graph[1]["body"]["workspace_id"] = uid()
        if invalid == "missing":
            graph[1]["body"]["depends_on"] = [uid()]
        if invalid == "unknown_role":
            graph[1]["body"]["role"] = "ADMIN"
        with repo.edits.write() as db, pytest.raises(EditError):
            install_board(db, graph_run, scope, graph)
    with repo.sessions() as db:
        assert db.execute(select(tasks.c.id).where(tasks.c.run_id == graph_run)).first() is None
    run = repo.create(
        scope,
        dict(scope=dict(project_id=project), intent="EDIT", message="剪辑", idempotency_key="new-team"),
        team=True,
    )["run_id"]
    first = repo.claim(uid())
    with repo.edits.write() as db:
        with pytest.raises(EditError):
            repo.writer_guard(db, first, "COMMIT")
    repo.advance(first, state=dict(phase="MATERIAL"), docs={"brief": {"description": "source material"}})
    material = repo.claim(uid())
    inbox = repo.page(scope, run, mail, None, 10)["items"]
    assert [item["sequence"] for item in inbox] == [1]
    validate_contract("MailboxEvent", inbox[0])
    with repo.edits.write() as db:
        assert db.execute(
            select(deliveries.c.accepted).where(deliveries.c.id == inbox[0]["message_id"])
        ).scalar()
        db.execute(update(runs).where(runs.c.id == run).values(lease=0))
    takeover = WorkflowRepository(repo.edits).claim(uid())
    assert takeover.fence > material.fence
    with pytest.raises(StaleLease):
        repo.advance(material, state=dict(phase="COMMIT"))
    repo.advance(takeover, state=dict(phase="PROPOSE"))
    propose = repo.claim(uid())
    with repo.edits.write() as db:
        with pytest.raises(EditError):
            repo.writer_guard(db, propose, "COMMIT")
    repo.advance(
        propose, state=dict(phase="COMMIT"), docs={"proposal": {"brief_id": inbox[0]["artifact_ids"][0]}}
    )
    coordinator = repo.claim(uid())
    with repo.edits.write() as db:
        repo.writer_guard(db, coordinator, "COMMIT")
    repo.control(scope, run, "cancel", {"idempotency_key": "cancel-team"})
    with repo.edits.write() as db:
        with pytest.raises(StaleLease):
            repo.writer_guard(db, coordinator, "COMMIT")
        assert db.execute(select(attempts.c.id).where(attempts.c.outcome == "EXPIRED")).first()
    final = repo.page(scope, run, tasks, None, 10)["items"]
    assert all(t["status"] in ("CANCELLED", "SUCCEEDED") for t in final)
    assert all(t["role"] in ("COORDINATOR", "MATERIAL", "EDITOR", "REVIEWER") for t in final)
    all_mail = repo.page(scope, run, mail, None, 20)["items"]
    assert any(msg["type"] == "CANCEL" for msg in all_mail)
    assert len({msg["message_id"] for msg in all_mail}) == len(all_mail)
    for t in final:
        validate_contract("TeamTask", t)
    engine.dispose()
