"""One deployment-owned migration path; application startup only checks head."""
import os

from alembic import context

from cutsieve.infrastructure.persistence import (
    edit_tables,
    processing_tables,
    search_tables,
    source_tables,
    workflow_tables,
)
from cutsieve.infrastructure.persistence.sqlalchemy import Base, create_sqlalchemy_engine

config = context.config


def run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata,
                      compare_type=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=os.environ.get("CUTSIEVE_DATABASE_URL") or config.get_main_option("sqlalchemy.url"))
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    run(config.attributes["connection"])
else:
    url = os.environ.get("CUTSIEVE_DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("CUTSIEVE_DATABASE_URL is required for migrations")
    engine = create_sqlalchemy_engine(url)
    try:
        with engine.connect() as connection:
            run(connection)
    finally:
        engine.dispose()
