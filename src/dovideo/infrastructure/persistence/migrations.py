"""Read-only startup guard and explicit Alembic test/deployment helpers."""
from pathlib import Path
import sysconfig

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory


def create_or_adopt_table(name, *definition):
    """Create a migration's table, or verify an existing unversioned copy."""
    from alembic import op
    from alembic.autogenerate import compare_metadata
    from sqlalchemy import MetaData, Table, inspect

    connection = op.get_bind()
    if not inspect(connection).has_table(name):
        op.create_table(name, *definition)
        return
    expected = MetaData()
    # Reflection resolves the referenced tables needed to compare foreign keys.
    expected.reflect(connection, only=[name])
    expected.remove(expected.tables[name])
    Table(name, expected, *definition)
    context = MigrationContext.configure(connection, opts={
        "include_object": lambda obj, key, kind, reflected, other: kind != "table" or key == name,
        "compare_type": True,
    })
    if compare_metadata(context, expected):
        raise RuntimeError(f"Existing {name} does not match the migration; inspect it before upgrading")


def migration_config():
    root = Path(__file__).resolve().parents[4]
    if not (root / "alembic.ini").is_file():
        root = Path(sysconfig.get_path("data")) / "share/videomind"
    return Config(str(root / "alembic.ini"))


def upgrade_schema(engine):
    config = migration_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.commit()


def require_schema_head(engine):
    heads = set(ScriptDirectory.from_config(migration_config()).get_heads())
    with engine.connect() as connection:
        current = set(MigrationContext.configure(connection).get_current_heads())
    if current != heads:
        raise RuntimeError("Database schema is not at Alembic head; run alembic upgrade head before startup")
