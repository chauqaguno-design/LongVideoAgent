"""Persist query images and full occurrence lists."""

from alembic import op
import sqlalchemy as sa
from dovideo.infrastructure.persistence.migrations import create_or_adopt_table

revision = "0008_search"
down_revision = "0007_processing"
branch_labels = depends_on = None


def upgrade():
    create_or_adopt_table(
        "vm_search_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace", sa.String(36), nullable=False, index=True),
        sa.Column("project", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("mime", sa.String(80), nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("media_ids", sa.JSON, nullable=False),
    )
    create_or_adopt_table(
        "vm_query_scans",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("workspace", sa.String(36), nullable=False),
        sa.Column("project", sa.String(36), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("snapshot", sa.JSON, nullable=False),
        sa.Column("answer", sa.JSON(none_as_null=True)),
    )


def downgrade():
    op.drop_table("vm_query_scans")
    op.drop_table("vm_search_artifacts")
