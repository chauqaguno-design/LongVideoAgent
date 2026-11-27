"""Create the workspace spending ledger."""

from alembic import op
from cutsieve.infrastructure.persistence.workspace_budget_table import define_v1

revision = "0012_workspace_budget"
down_revision = "0010_shared_outbox"
branch_labels = depends_on = None


def upgrade():
    define_v1(op.create_table)
def downgrade():
    op.drop_table("vm_workspace_model_budgets")
