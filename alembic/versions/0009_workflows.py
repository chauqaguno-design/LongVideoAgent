"""Durable agent run, receipts, result artifacts."""

from alembic import op
from dovideo.infrastructure.persistence.workflow_tables import define_v1

revision = "0009_workflows"
down_revision = "0008_search"
branch_labels = depends_on = None


def upgrade():
    define_v1(op.create_table)


def downgrade():
    for name in reversed(define_v1(lambda name, *columns: name)):
        op.drop_table(name)
