"""A run checkpoint and its sequenced events share one transaction."""

from sqlalchemy import BigInteger, Column, ForeignKey, Integer, JSON, String, Table, UniqueConstraint
from .edit_tables import identifier
from .sqlalchemy import Base
from .workspace_budget_table import workspace_budgets


def define_v1(table):
    runs = table(
        "vm_workflow_runs",
        identifier("id", primary_key=True),
        identifier("workspace_id"),
        identifier("project_id"),
        Column("status", String(20), nullable=False, index=True),
        Column("lease", BigInteger, nullable=False),
        Column("fence", Integer, nullable=False),
        Column("ready_at", BigInteger, nullable=False),
        Column("deadline", BigInteger, nullable=False),
        Column("state", JSON, nullable=False),
        Column("checkpoint", JSON, nullable=False),
    )

    def run_ref():
        return Column("run_id", String(36), ForeignKey("vm_workflow_runs.id"), nullable=False)

    events = table(
        "vm_workflow_events",
        identifier("id", primary_key=True),
        run_ref(),
        Column("seq", Integer, nullable=False),
        Column("body", JSON, nullable=False),
        UniqueConstraint("run_id", "seq"),
    )

    calls = table(
        "vm_workflow_calls",
        run_ref(),
        Column("call_key", String(80), nullable=False),
        Column("name", String(80), nullable=False),
        Column("attempts", Integer, nullable=False),
        Column("input_digest", String(64), nullable=False),
        Column("response", JSON(none_as_null=True)),
        UniqueConstraint("run_id", "call_key"),
    )

    documents = table(
        "vm_workflow_documents",
        identifier("id", primary_key=True),
        run_ref(),
        Column("kind", String(20), nullable=False),
        Column("body", JSON, nullable=False),
    )

    tasks = table(
        "vm_team_tasks",
        identifier("id", primary_key=True),
        run_ref(),
        Column("stage", String(24), nullable=False),
        Column("position", Integer, nullable=False),
        Column("body", JSON, nullable=False),
        UniqueConstraint("run_id", "stage"),
    )

    mail = table(
        "vm_team_mail",
        identifier("id", primary_key=True),
        run_ref(),
        Column("seq", Integer, nullable=False),
        Column("body", JSON, nullable=False),
        UniqueConstraint("run_id", "seq"),
    )

    model_requests = table(
        "vm_workflow_model_requests",
        identifier("id", primary_key=True),
        run_ref(),
        Column("step", String(64), nullable=False),
        Column("price", JSON, nullable=False),
        Column("reserved", BigInteger, nullable=False),
        Column("status", String(16), nullable=False),
        Column("response", JSON(none_as_null=True)),
    )

    attempts = table(
        "vm_team_attempts",
        identifier("id", primary_key=True),
        run_ref(),
        identifier("task_id"),
        identifier("owner"),
        Column("fence", Integer, nullable=False),
        Column("start_at", BigInteger, nullable=False),
        Column("end_at", BigInteger),
        Column("outcome", String(20)),
        UniqueConstraint("task_id", "fence"),
    )

    deliveries = table(
        "vm_team_inbox",
        identifier("id", primary_key=True),
        run_ref(),
        identifier("recipient"),
        Column("expires", BigInteger, nullable=False),
        Column("accepted", BigInteger),
    )
    return [runs, events, calls, documents, tasks, mail, model_requests, attempts, deliveries]


WORKFLOW_TABLES = define_v1(lambda name, *cols: Table(name, Base.metadata, *cols))
runs, events, calls, documents, tasks, mail, model_requests, attempts, deliveries = WORKFLOW_TABLES
WORKFLOW_TABLES.append(workspace_budgets)
