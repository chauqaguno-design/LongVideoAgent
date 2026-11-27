"""SQL layout of the v1 processing queue, immutable artifacts and observations."""

from sqlalchemy import BigInteger, Column, ForeignKey, Integer, JSON, String, Table, UniqueConstraint

from .sqlalchemy import Base


def define_v1(table):
    def key(name, **options):
        return Column(name, String(36), nullable=False, **options)

    runs = table(
        "vm_processing_runs",
        key("id", primary_key=True),
        key("workspace", index=True),
        key("project"),
        key("asset", index=True),
        key("revision"),
        Column("predecessor", String(36)),
        Column("kind", String(16), nullable=False),
        Column("status", String(16), nullable=False, index=True),
        Column("stage", String(40), nullable=False),
        Column("config", JSON, nullable=False),
        Column("completed", Integer, nullable=False),
        Column("total", Integer),
        Column("attempt", Integer, nullable=False),
        Column("lease_end", BigInteger, nullable=False),
        Column("created", String(40), nullable=False),
        Column("cancelled", String(40)),
        Column("error", JSON(none_as_null=True)),
    )
    records = table(
        "vm_processing_records",
        key("id", primary_key=True),
        key("workspace", index=True),
        key("asset", index=True),
        Column("run", String(36), ForeignKey("vm_processing_runs.id"), nullable=False, index=True),
        Column("kind", String(20), nullable=False),
        Column("unit", String(100), nullable=False),
        Column("start_us", BigInteger, nullable=False),
        Column("body", JSON, nullable=False),
        UniqueConstraint("run", "kind", "unit"),
    )
    return runs, records


runs, records = define_v1(lambda name, *cols: Table(name, Base.metadata, *cols))
