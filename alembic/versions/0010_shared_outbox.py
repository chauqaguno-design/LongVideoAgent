"""Add shared side-effect delivery and adopt team audit tables from local checkouts."""

from alembic import op
from dovideo.infrastructure.persistence.migrations import create_or_adopt_table
from dovideo.infrastructure.persistence.outbox_tables import define_v1
from dovideo.infrastructure.persistence.workflow_tables import define_v1 as workflow_tables

revision = "0010_shared_outbox"
down_revision = "0009_workflows"
branch_labels = depends_on = None


def upgrade():
    define_v1(create_or_adopt_table)

    # Early 0009 installs could precede detailed per-attempt history.
    def adopt_audit(name, *cols):
        if name in ("vm_workflow_model_requests", "vm_team_attempts", "vm_team_inbox"):
            create_or_adopt_table(name, *cols)

    workflow_tables(adopt_audit)


def downgrade():
    op.drop_table("vm_event_inbox")
    op.drop_table("vm_outbox")
