from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from passagen.storage.migrations import SCHEMA_VERSION as STORAGE_SCHEMA_VERSION
from passagen.storage.migrations import SchemaVersionError, initialize_schema, schema_version

SCHEMA_VERSION = STORAGE_SCHEMA_VERSION


class DatabaseVersionError(RuntimeError):
    pass


def backup_database(database_path: Path, destination: Path) -> Path:
    source = database_path.expanduser().resolve()
    target = destination.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Database is not initialized: {source}")
    if source == target:
        raise ValueError("Backup destination must differ from the active database")
    if target.exists():
        raise FileExistsError(f"Backup destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_connection, sqlite3.connect(target) as target_connection:
        source_connection.backup(target_connection)
    return target


def initialize_database(database_path: Path) -> None:
    try:
        initialize_schema(database_path)
    except SchemaVersionError as exc:
        raise DatabaseVersionError(str(exc)) from exc


def current_version(database_path: Path) -> int | None:
    return schema_version(database_path)


@contextmanager
def connect_database(database_path: Path) -> Iterator[sqlite3.Connection]:
    database_path = database_path.expanduser().resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
