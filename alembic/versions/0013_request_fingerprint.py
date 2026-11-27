"""Bind persisted model responses to their physical request input."""

from alembic import op
import sqlalchemy as sa

revision = "0013_request_fingerprint"
down_revision = "0012_workspace_budget"
branch_labels = depends_on = None


def upgrade():
    op.add_column("vm_workflow_model_requests", sa.Column("request_fingerprint", sa.String(64), nullable=False))


def downgrade():
    op.drop_column("vm_workflow_model_requests", "request_fingerprint")
