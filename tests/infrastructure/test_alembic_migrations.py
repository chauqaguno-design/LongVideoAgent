from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
import pytest
from sqlalchemy import inspect

from dovideo.infrastructure.persistence.migrations import require_schema_head, upgrade_schema
from dovideo.infrastructure.persistence.sqlalchemy import Base, create_sqlalchemy_engine


@pytest.fixture
def engine(tmp_path):
    value = create_sqlalchemy_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    yield value
    value.dispose()


def assert_no_drift(engine):
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata) == []


def test_empty_upgrade_repeat_and_no_model_drift(engine):
    with pytest.raises(RuntimeError, match="upgrade head"):
        require_schema_head(engine)
    upgrade_schema(engine)
    upgrade_schema(engine)
    require_schema_head(engine)
    assert_no_drift(engine)
    assert "request_fingerprint" in {c["name"] for c in inspect(engine).get_columns("vm_workflow_model_requests")}
    assert "vm_object_parts" not in inspect(engine).get_table_names()


def test_head_check_never_performs_ddl(engine):
    upgrade_schema(engine)
    before = inspect(engine).get_table_names()
    require_schema_head(engine)
    assert inspect(engine).get_table_names() == before
