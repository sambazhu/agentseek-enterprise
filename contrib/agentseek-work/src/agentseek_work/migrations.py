from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Connection, Engine, insert, inspect, select

from agentseek_work.schema import metadata, schema_versions

LATEST_SCHEMA_VERSION = 4


def apply_migrations(engine: Engine) -> int:
    """Apply the initial work-ledger schema and return its current version."""
    with engine.begin() as connection:
        metadata.create_all(connection)
        current = _current_version(connection)
        for version in range(current + 1, LATEST_SCHEMA_VERSION + 1):
            _apply_revision(connection, version)
            connection.execute(insert(schema_versions).values(version=version, applied_at=datetime.now(tz=UTC)))
            current = version
        if current > LATEST_SCHEMA_VERSION:
            raise RuntimeError(f"database schema version {current} is newer than supported {LATEST_SCHEMA_VERSION}")
        return current


def current_schema_version(engine: Engine) -> int:
    """Return zero when the ledger is absent, without mutating the database."""

    if not inspect(engine).has_table(schema_versions.name):
        return 0
    with engine.connect() as connection:
        return _current_version(connection)


def _current_version(connection: Connection) -> int:
    value = connection.execute(select(schema_versions.c.version).order_by(schema_versions.c.version.desc())).scalar()
    return int(value or 0)


def _apply_revision(connection: Connection, version: int) -> None:
    if version != 4:
        return
    existing = {str(column["name"]) for column in inspect(connection).get_columns("enterprise_work_items")}
    additions = {
        "digital_employee_profile_version": "VARCHAR(64)",
        "digital_employee_permissions_digest": "VARCHAR(160)",
    }
    for column_name, column_type in additions.items():
        if column_name not in existing:
            connection.exec_driver_sql(f"ALTER TABLE enterprise_work_items ADD COLUMN {column_name} {column_type}")
