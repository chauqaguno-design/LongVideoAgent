"""Add shared side-effect delivery."""

from alembic import op
from dovideo.infrastructure.persistence.outbox_tables import define_v1

revision = "0010_shared_outbox"
down_revision = "0009_workflows"
branch_labels = depends_on = None


def upgrade():
    define_v1(op.create_table)

def downgrade():
    op.drop_table("vm_event_inbox")
    op.drop_table("vm_outbox")
