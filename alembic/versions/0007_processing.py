"""Durable media processing runs, coverage and evidence records."""

from alembic import op
from cutsieve.infrastructure.persistence.processing_tables import define_v1

revision = "0007_processing"
down_revision = "0006_edit_timeline"
branch_labels = depends_on = None


def upgrade():
    define_v1(op.create_table)


def downgrade():
    op.drop_table("vm_processing_records")
    op.drop_table("vm_processing_runs")
