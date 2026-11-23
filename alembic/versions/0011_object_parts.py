"""Acknowledge MinIO multipart upload receipts durably across application replicas."""

from alembic import op
from dovideo.infrastructure.persistence.migrations import create_or_adopt_table
from dovideo.infrastructure.persistence.object_parts_table import define_v1

revision = "0011_object_parts"
down_revision = "0010_shared_outbox"
branch_labels = depends_on = None


def upgrade():
    define_v1(create_or_adopt_table)


def downgrade():
    op.drop_table("vm_object_upload_parts")
