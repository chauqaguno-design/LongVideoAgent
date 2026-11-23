"""Carry active and historical run liabilities into the workspace spending boundary."""

from collections import Counter
from alembic import op
from sqlalchemy import insert, select
from dovideo.infrastructure.persistence.migrations import create_or_adopt_table
from dovideo.infrastructure.persistence.workspace_budget_table import define_v1, workspace_budgets
from dovideo.infrastructure.persistence.workflow_tables import runs

revision = "0012_workspace_budget"
down_revision = "0011_object_parts"
branch_labels = depends_on = None


def upgrade():
    define_v1(create_or_adopt_table)
    db = op.get_bind()
    totals = Counter()
    for workspace, state in db.execute(select(runs.c.workspace_id, runs.c.state)):
        usage = state["budget_usage"]
        totals[workspace, usage["currency"]] += sum(
            usage[key] or 0 for key in ("committed_microunits", "reserved_microunits", "uncertain_microunits")
        )
    existing = set(db.execute(select(workspace_budgets.c.workspace, workspace_budgets.c.currency)).all())
    for (workspace, currency), amount in totals.items():
        if (workspace, currency) not in existing:
            db.execute(
                insert(workspace_budgets).values(workspace=workspace, currency=currency, exposure=amount)
            )


def downgrade():
    op.drop_table("vm_workspace_model_budgets")
