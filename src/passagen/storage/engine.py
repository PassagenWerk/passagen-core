from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool


@lru_cache(maxsize=32)
def _engine_for_path(resolved_path: str) -> Engine:
    engine = create_engine(
        f"sqlite+pysqlite:///{resolved_path}",
        poolclass=NullPool,
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA busy_timeout = 5000")
        finally:
            cursor.close()

    return engine


def database_engine(database_path: Path) -> Engine:
    path = database_path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return _engine_for_path(str(path))


@contextmanager
def session_scope(database_path: Path) -> Iterator[Session]:
    with (
        Session(database_engine(database_path), expire_on_commit=False) as session,
        session.begin(),
    ):
        yield session
