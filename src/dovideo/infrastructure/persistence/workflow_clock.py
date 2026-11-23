"""Lease and deadline decisions use one database clock across workers."""

from sqlalchemy import func, select


def db_now(session):
    clock = (
        func.unix_timestamp() if session.get_bind().dialect.name == "mysql" else func.strftime("%s", "now")
    )
    return int(session.scalar(select(clock)))
