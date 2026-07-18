from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Connection, Engine, func, insert, inspect, select

from agentseek_work.models import TERMINAL_WORK_STATUSES
from agentseek_work.schema import (
    metadata,
    schema_versions,
    work_claim_evidence,
    work_claims,
    work_contracts,
    work_evidence,
    work_items,
    work_sources,
)

LATEST_SCHEMA_VERSION = 8

_ACTIVE_PLAYBOOK_INDEX = "uq_work_items_active_playbook"
_CURRENT_CONTRACT_INDEX = "uq_work_contracts_current_type"


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
    elif version == 6:
        _apply_revision_six(connection)
    elif version == 7:
        _apply_revision_seven(connection)
    elif version == 8:
        _apply_revision_eight(connection)


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


def _apply_revision_six(connection: Connection) -> None:
    if not inspect(connection).has_table(work_contracts.name):
        raise RuntimeError("cannot apply work schema revision 6; enterprise_work_contracts is missing")
    existing = {str(column["name"]) for column in inspect(connection).get_columns(work_contracts.name)}
    required = {
        "work_id",
        "tenant_id",
        "contract_type",
        "contract_version",
        "status",
        "payload",
        "created_by",
        "created_at",
        "confirmed_by",
        "confirmed_at",
        "superseded_at",
    }
    missing = sorted(required - existing)
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"cannot apply work schema revision 6; enterprise_work_contracts is missing: {joined}")
    duplicate_contracts = (
        select(work_contracts.c.work_id, work_contracts.c.contract_type)
        .where(work_contracts.c.status != "superseded")
        .group_by(work_contracts.c.work_id, work_contracts.c.contract_type)
        .having(func.count() > 1)
        .subquery()
    )
    duplicate_count = connection.execute(select(func.count()).select_from(duplicate_contracts)).scalar_one()
    if duplicate_count:
        raise RuntimeError(
            "cannot apply work schema revision 6; "
            f"found {duplicate_count} WorkItem contract type(s) with multiple current versions"
        )
    indexes = {str(index["name"]) for index in inspect(connection).get_indexes(work_contracts.name)}
    if _CURRENT_CONTRACT_INDEX not in indexes:
        connection.exec_driver_sql(
            f"CREATE UNIQUE INDEX {_CURRENT_CONTRACT_INDEX} "
            f"ON {work_contracts.name} (work_id, contract_type) "
            "WHERE status <> 'superseded'"
        )


def _apply_revision_seven(connection: Connection) -> None:
    if not inspect(connection).has_table(work_sources.name):
        raise RuntimeError("cannot apply work schema revision 7; enterprise_work_sources is missing")
    existing = {str(column["name"]) for column in inspect(connection).get_columns(work_sources.name)}
    required = {
        "source_id",
        "work_id",
        "tenant_id",
        "source_type",
        "title",
        "publisher",
        "published_at",
        "retrieved_at",
        "locator",
        "uri_digest",
        "file_id",
        "confidentiality_level",
        "authority_level",
        "allowed_uses",
        "content_hash",
        "result_digest",
        "snapshot_policy",
        "snapshot_status",
        "snapshot_artifact_id",
        "license_restriction",
        "retrieval_query_digest",
        "license_terms_ref",
        "excerpt_status",
        "metadata",
    }
    missing = sorted(required - existing)
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"cannot apply work schema revision 7; enterprise_work_sources is missing: {joined}")


def _apply_revision_eight(connection: Connection) -> None:
    required_tables = {
        work_evidence.name: {
            "evidence_id",
            "work_id",
            "tenant_id",
            "source_id",
            "locator",
            "excerpt",
            "structured_value",
            "unit",
            "period",
            "confidence",
            "extraction_method",
            "created_at",
            "metadata",
        },
        work_claims.name: {
            "claim_id",
            "work_id",
            "tenant_id",
            "section_id",
            "statement",
            "claim_type",
            "verification_status",
            "reviewer_status",
            "created_at",
            "metadata",
        },
        work_claim_evidence.name: {"claim_id", "evidence_id", "ordinal"},
    }
    inspector = inspect(connection)
    for table_name, required_columns in required_tables.items():
        if not inspector.has_table(table_name):
            raise RuntimeError(f"cannot apply work schema revision 8; {table_name} is missing")
        existing = {str(column["name"]) for column in inspector.get_columns(table_name)}
        missing = sorted(required_columns - existing)
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"cannot apply work schema revision 8; {table_name} is missing: {joined}")
