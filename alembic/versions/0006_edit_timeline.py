"""Add revisioned edit projects, operation receipts and render jobs."""
from alembic import op
import sqlalchemy as sa

revision = "0006_edit_timeline"
down_revision = "0005_edit_sources"
branch_labels = None
depends_on = None


def ident(name, **kwargs):
    return sa.Column(name, sa.String(36), nullable=False, **kwargs)


def upgrade():
    op.create_table("vm_edit_projects",
        ident("id", primary_key=True), ident("workspace_id"), ident("project_id"),
        sa.Column("title", sa.String(160), nullable=False), sa.Column("head", sa.Integer, nullable=False),
        ident("head_revision_id"))
    op.create_table("vm_timeline_revisions",
        ident("id", primary_key=True), ident("workspace_id"),
        sa.Column("edit_id", sa.String(36), sa.ForeignKey("vm_edit_projects.id"), nullable=False),
        sa.Column("number", sa.Integer, nullable=False), sa.Column("snapshot", sa.JSON, nullable=False),
        sa.UniqueConstraint("edit_id","number"))
    op.create_table("vm_edit_receipts",
        ident("workspace_id", primary_key=True), ident("scope_id", primary_key=True),
        sa.Column("kind", sa.String(16), primary_key=True), sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False), sa.Column("result", sa.JSON, nullable=False))
    op.create_table("vm_edit_events",
        ident("id", primary_key=True), ident("workspace_id"), ident("edit_id"), ident("revision_id"),
        sa.Column("reason", sa.String(2000), nullable=False), sa.Column("operations", sa.JSON, nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False))
    op.create_table("vm_render_jobs",
        ident("id", primary_key=True), ident("workspace_id"), ident("edit_id"),
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("vm_timeline_revisions.id"), nullable=False),
        sa.Column("profile", sa.String(12), nullable=False), sa.Column("cache_key", sa.String(64), nullable=False),
        sa.Column("cache_slot", sa.String(64)), sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("attempt", sa.Integer, nullable=False), sa.Column("lease_until", sa.BigInteger, nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False), sa.Column("result", sa.JSON, nullable=False),
        sa.UniqueConstraint("workspace_id", "cache_slot"))
    op.create_table("vm_render_artifacts",
        ident("id", primary_key=True), ident("workspace_id"), ident("revision_id"),
        sa.Column("kind", sa.String(24), nullable=False), sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False), sa.Column("relative_path", sa.String(200), nullable=False))


def downgrade():
    for name in ("vm_render_artifacts", "vm_render_jobs", "vm_edit_events", "vm_edit_receipts",
                 "vm_timeline_revisions", "vm_edit_projects"):
        op.drop_table(name)
