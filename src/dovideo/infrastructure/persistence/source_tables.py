from sqlalchemy import BigInteger, Column, ForeignKey, JSON, String, Table

from .sqlalchemy import Base


projects = Table("vm_projects", Base.metadata,
    Column("id", String(36), primary_key=True),
    Column("workspace", String(36), nullable=False, index=True),
    Column("title", String(160), nullable=False),
    Column("created", String(40), nullable=False),
)

# One upload creates one immutable asset and source revision in v1.
sources = Table("vm_sources", Base.metadata,
    Column("id", String(36), primary_key=True),
    Column("project", String(36), ForeignKey("vm_projects.id"), nullable=False, index=True),
    Column("workspace", String(36), nullable=False, index=True),
    Column("revision", String(36), unique=True, nullable=False),
    Column("artifact", String(36), unique=True, nullable=False),
    Column("upload", String(36), unique=True, nullable=False),
    Column("filename", String(255), nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("mime", String(255), nullable=False),
    Column("expected_digest", String(64)),
    Column("digest", String(64)),
    Column("expires", BigInteger, nullable=False),
    Column("probe", JSON(none_as_null=True)),
    Column("receipt", String(64)),
)
