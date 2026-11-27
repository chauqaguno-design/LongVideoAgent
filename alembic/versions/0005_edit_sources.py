"""Add scoped editing projects and immutable media revisions."""
from alembic import op
import sqlalchemy as sa

revision = "0005_edit_sources"
down_revision = "0001_schema_baseline"
branch_labels = depends_on = None


def upgrade():
    op.create_table("vm_projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace", sa.String(36), nullable=False, index=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("created", sa.String(40), nullable=False))
    op.create_table("vm_sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project", sa.String(36), sa.ForeignKey("vm_projects.id"), nullable=False, index=True),
        sa.Column("workspace", sa.String(36), nullable=False, index=True),
        *[sa.Column(name, sa.String(36), nullable=False, unique=True) for name in ("revision", "artifact", "upload")],
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mime", sa.String(255), nullable=False),
        sa.Column("expected_digest", sa.String(64)), sa.Column("digest", sa.String(64)),
        sa.Column("expires", sa.BigInteger(), nullable=False),
        sa.Column("probe", sa.JSON(none_as_null=True)), sa.Column("receipt", sa.String(64)))


def downgrade():
    op.drop_table("vm_sources")
    op.drop_table("vm_projects")
