"""Acknowledged immutable upload parts in an object-backed workspace."""

from sqlalchemy import BigInteger, Column, Integer, String, Table
from .sqlalchemy import Base


def define_v1(table):
    return table(
        "vm_object_upload_parts",
        Column("workspace", String(36), nullable=False, index=True),
        Column("upload", String(36), primary_key=True),
        Column("number", Integer, primary_key=True),
        Column("digest", String(64), nullable=False),
        Column("size", BigInteger, nullable=False),
    )


object_parts = define_v1(lambda name, *cols: Table(name, Base.metadata, *cols))
