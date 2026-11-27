"""Immutable edit revisions and the durable render queue."""
from sqlalchemy import BigInteger, Column, ForeignKey, Integer, JSON, String, Table, UniqueConstraint

from .sqlalchemy import Base
from .outbox_tables import outbox, inbox


def identifier(name, **kwargs):
    return Column(name, String(36), nullable=False, **kwargs)


projects = Table(
    "vm_edit_projects", Base.metadata,
    identifier("id", primary_key=True), identifier("workspace_id"), identifier("project_id"),
    Column("title", String(160), nullable=False),
    Column("head", Integer, nullable=False), identifier("head_revision_id"),
)
revisions = Table(
    "vm_timeline_revisions", Base.metadata,
    identifier("id", primary_key=True), identifier("workspace_id"),
    Column("edit_id", String(36), ForeignKey(projects.c.id), nullable=False),
    Column("number", Integer, nullable=False), Column("snapshot", JSON, nullable=False),
    UniqueConstraint("edit_id", "number"),
)
receipts = Table(
    "vm_edit_receipts", Base.metadata,
    identifier("workspace_id", primary_key=True), identifier("scope_id", primary_key=True),
    Column("kind", String(16), primary_key=True), Column("key", String(128), primary_key=True),
    Column("digest", String(64), nullable=False), Column("result", JSON, nullable=False),
)
events = Table(
    "vm_edit_events", Base.metadata, identifier("id", primary_key=True),
    identifier("workspace_id"), identifier("edit_id"), identifier("revision_id"),
    Column("reason", String(2000), nullable=False),
    Column("operations", JSON, nullable=False), Column("created_at", String(40), nullable=False),
)
jobs = Table(
    "vm_render_jobs", Base.metadata,
    identifier("id", primary_key=True), identifier("workspace_id"), identifier("edit_id"),
    Column("revision_id", String(36), ForeignKey(revisions.c.id), nullable=False),
    Column("profile", String(12), nullable=False), Column("cache_key", String(64), nullable=False),
    Column("cache_slot", String(64)),
    Column("status", String(16), nullable=False, index=True),
    Column("attempt", Integer, nullable=False), Column("lease_until", BigInteger, nullable=False),
    Column("created_at", String(40), nullable=False), Column("result", JSON, nullable=False),
    UniqueConstraint("workspace_id", "cache_slot"),
)
artifacts = Table(
    "vm_render_artifacts", Base.metadata,
    identifier("id", primary_key=True), identifier("workspace_id"), identifier("revision_id"),
    Column("kind", String(24), nullable=False), Column("digest", String(64), nullable=False),
    Column("size", BigInteger, nullable=False), Column("relative_path", String(200), nullable=False),
)

EDIT_TABLES = [projects, revisions, receipts, events, jobs, artifacts, outbox, inbox]
