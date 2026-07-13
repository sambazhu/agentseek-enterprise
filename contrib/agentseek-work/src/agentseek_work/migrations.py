from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Connection, Engine, insert, select

from agentseek_work.schema import metadata, schema_versions

LATEST_SCHEMA_VERSION = 2


def apply_migrations(engine: Engine) -> int:
    """Apply the initial work-ledger schema and return its current version."""
    with engine.begin() as connection:
        metadata.create_all(connection)
        current = _current_version(connection)
        for version in range(current + 1, LATEST_SCHEMA_VERSION + 1):
            connection.execute(insert(schema_versions).values(version=version, applied_at=datetime.now(tz=UTC)))
            current = version
        if current > LATEST_SCHEMA_VERSION:
            raise RuntimeError(f"database schema version {current} is newer than supported {LATEST_SCHEMA_VERSION}")
        return current


def _current_version(connection: Connection) -> int:
    value = connection.execute(select(schema_versions.c.version).order_by(schema_versions.c.version.desc())).scalar()
    return int(value or 0)
