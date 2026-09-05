from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect

from passagen.storage.engine import database_engine

SCHEMA_VERSION = 4
_BASELINE_REVISION = "0001"
_APPLICATION_TABLES = {"papers", "artifacts", "processing_runs", "llm_calls"}
_REQUIRED_COLUMNS = {
    "papers": {"id", "original_filename", "pdf_sha256", "status"},
    "artifacts": {"id", "paper_id", "kind", "path"},
    "processing_runs": {"id", "paper_id", "stage", "status"},
    "llm_calls": {"id", "processing_run_id", "provider", "model"},
}


class SchemaVersionError(RuntimeError):
    pass


def initialize_schema(database_path: Path) -> None:
    engine = database_engine(database_path)
    with engine.connect() as connection:
        user_version = _user_version(connection)
        if user_version > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Database schema {user_version} is newer than supported {SCHEMA_VERSION}"
            )
        tables = set(inspect(connection).get_table_names())
        application_tables = tables & _APPLICATION_TABLES
        has_alembic = "alembic_version" in tables
        connection.commit()

        config = _config(connection)
        if not application_tables and not has_alembic:
            command.upgrade(config, "head")
        elif (
            application_tables == _APPLICATION_TABLES
            and not has_alembic
            and user_version
            in {
                1,
                SCHEMA_VERSION,
            }
        ):
            _validate_legacy_schema(connection)
            command.stamp(config, _BASELINE_REVISION if user_version == 1 else head_revision())
            command.upgrade(config, "head")
        elif has_alembic:
            command.upgrade(config, "head")
        else:
            raise SchemaVersionError(
                "Database schema is incomplete or unsupported; refusing to modify it"
            )
        connection.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()


def schema_version(database_path: Path) -> int | None:
    path = database_path.expanduser().resolve()
    if not path.exists():
        return None
    with database_engine(path).connect() as connection:
        return _user_version(connection)


def alembic_revision(database_path: Path) -> str | None:
    with database_engine(database_path).connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def head_revision() -> str:
    return str(ScriptDirectory.from_config(_config()).get_current_head())


def _validate_legacy_schema(connection: Connection) -> None:
    inspector = inspect(connection)
    for table, required in _REQUIRED_COLUMNS.items():
        columns = {str(column["name"]) for column in inspector.get_columns(table)}
        if not required <= columns:
            raise SchemaVersionError(f"Legacy table {table} is missing required columns")
    quick_check = connection.exec_driver_sql("PRAGMA quick_check").scalar_one()
    foreign_key_issues = connection.exec_driver_sql("PRAGMA foreign_key_check").first()
    connection.commit()
    if quick_check != "ok" or foreign_key_issues is not None:
        raise SchemaVersionError("Legacy database failed SQLite integrity checks")


def _config(connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).with_name("alembic")))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def _user_version(connection: Connection) -> int:
    return int(connection.exec_driver_sql("PRAGMA user_version").scalar_one())
