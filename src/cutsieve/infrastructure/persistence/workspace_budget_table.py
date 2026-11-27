"""The SQL balance includes known, reserved and uncertain model spending in one currency."""

from sqlalchemy import BigInteger, Column, String, Table
from .sqlalchemy import Base


def define_v1(table):
    return table(
        "vm_workspace_model_budgets",
        Column("workspace", String(36), primary_key=True),
        Column("currency", String(3), primary_key=True),
        Column("exposure", BigInteger, nullable=False),
    )


workspace_budgets = define_v1(lambda name, *columns: Table(name, Base.metadata, *columns))
