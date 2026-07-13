from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Connection, Engine, func, insert, inspect, select

from agentseek_work.models import TERMINAL_WORK_STATUSES
from agentseek_work.schema import metadata, schema_versions, work_items

LATEST_SCHEMA_VERSION = 5

_ACTIVE_PLAYBOOK_INDEX = "uq_work_items_active_playbook"


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
    if version == 4:
        _apply_revision_four(connection)
    elif version == 5:
        _apply_revision_five(connection)


def _apply_revision_four(connection: Connection) -> None:
    existing = {str(column["name"]) for column in inspect(connection).get_columns(work_items.name)}
    additions = {
        "digital_employee_profile_version": "VARCHAR(64)",
        "digital_employee_permissions_digest": "VARCHAR(160)",
    }
    for column_name, column_type in additions.items():
        if column_name not in existing:
            connection.exec_driver_sql(f"ALTER TABLE {work_items.name} ADD COLUMN {column_name} {column_type}")


def _apply_revision_five(connection: Connection) -> None:
    existing = {str(column["name"]) for column in inspect(connection).get_columns(work_items.name)}
    scope_columns = {"tenant_id", "requester_id", "digital_employee_id", "playbook_id", "status"}
    missing = sorted(scope_columns - existing)
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"cannot apply work schema revision 5; enterprise_work_items is missing: {joined}")

    terminal_statuses = tuple(status.value for status in TERMINAL_WORK_STATUSES)
    duplicate_scopes = (
        select(
            work_items.c.tenant_id,
            work_items.c.requester_id,
            work_items.c.digital_employee_id,
            work_items.c.playbook_id,
        )
        .where(work_items.c.status.not_in(terminal_statuses))
        .group_by(
            work_items.c.tenant_id,
            work_items.c.requester_id,
            work_items.c.digital_employee_id,
            work_items.c.playbook_id,
        )
        .having(func.count() > 1)
        .subquery()
    )
    duplicate_count = connection.execute(select(func.count()).select_from(duplicate_scopes)).scalar_one()
    if duplicate_count:
        raise RuntimeError(
            "cannot apply work schema revision 5; "
            f"found {duplicate_count} active WorkItem scope(s) with duplicates"
        )

    # Migration-only by design: metadata.create_all() runs before revisions and
    # must not create this index before the duplicate-data preflight above.
    connection.exec_driver_sql(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_ACTIVE_PLAYBOOK_INDEX} "
        f"ON {work_items.name} (tenant_id, requester_id, digital_employee_id, playbook_id) "
        "WHERE status NOT IN ('succeeded', 'failed', 'cancelled')"
    )
