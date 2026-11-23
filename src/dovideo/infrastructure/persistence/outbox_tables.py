"""Shared event identifiers for render, workflow and team notification delivery."""

from sqlalchemy import BigInteger, Column, Integer, String, Table
from .sqlalchemy import Base


def define_v1(table):
    return (
        table(
            "vm_outbox",
            Column("id", String(36), primary_key=True),
            Column("workspace", String(36), nullable=False),
            Column("resource", String(36), nullable=False),
            Column("kind", String(40), nullable=False),
            Column("status", String(12), nullable=False, index=True),
            Column("available", BigInteger, nullable=False),
            Column("lease", BigInteger, nullable=False),
            Column("attempt", Integer, nullable=False),
            Column("last_error", String(30)),
        ),
        table(
            "vm_event_inbox",
            Column("id", String(36), primary_key=True),
            Column("accepted", BigInteger, nullable=False),
        ),
    )


outbox, inbox = define_v1(lambda name, *cols: Table(name, Base.metadata, *cols))
