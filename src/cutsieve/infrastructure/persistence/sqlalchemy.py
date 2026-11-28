"""Shared SQLAlchemy metadata and SQLite connection setup."""

from sqlalchemy import BigInteger, Integer, String, create_engine, event
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """CutSieve database metadata."""


class UserRow(Base):
    """Account table retained in the Alembic baseline."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    nickname: Mapped[str] = mapped_column(String(50), nullable=False)
    avatar: Mapped[str | None] = mapped_column(String(512), nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="USER")


def create_sqlalchemy_engine(database_url: str | URL) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        raise ValueError("A SQLite database URL is required")
    engine = create_engine(url)

    def application_tables(connection, schema=None, **kwargs):
        from .search_tables import FTS_TABLE

        names = sqlite_table_names(connection, schema=schema, **kwargs)
        auxiliary = {FTS_TABLE, *(f"{FTS_TABLE}_{suffix}" for suffix in
                                ("config", "content", "data", "docsize", "idx"))}
        return [name for name in names if name not in auxiliary]

    # Alembic uses the engine's table reflection. The virtual table and its
    # shadow tables are disposable search data, outside the managed schema.
    sqlite_table_names = engine.dialect.get_table_names
    engine.dialect.get_table_names = application_tables

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _record):
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    return engine
