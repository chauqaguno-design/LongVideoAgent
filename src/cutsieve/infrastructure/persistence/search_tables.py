"""Immutable user query files and their source access scope."""

from sqlalchemy import BigInteger, Column, JSON, String, Table

from .sqlalchemy import Base


artifacts = Table(
    "vm_search_artifacts",
    Base.metadata,
    Column("id", String(36), primary_key=True),
    Column("workspace", String(36), nullable=False, index=True),
    Column("project", String(36), nullable=False),
    Column("kind", String(24), nullable=False),
    Column("digest", String(64), nullable=False),
    Column("mime", String(80), nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("media_ids", JSON, nullable=False),
)

runs = Table(
    "vm_query_scans",
    Base.metadata,
    Column("run_id", String(36), primary_key=True),
    Column("workspace", String(36), nullable=False),
    Column("project", String(36), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("snapshot", JSON, nullable=False),
    Column("answer", JSON(none_as_null=True)),
)
