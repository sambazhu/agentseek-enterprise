from agentseek_work.schema import (
    pack_snapshots,
    work_budget_reservations,
    work_budget_usage,
    work_contracts,
    work_events,
    work_items,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable


def test_postgresql_work_items_ddl_uses_jsonb_and_idempotency_constraint() -> None:
    ddl = str(CreateTable(work_items).compile(dialect=postgresql.dialect()))

    assert "brief JSONB NOT NULL" in ddl
    assert "skill_digests JSONB NOT NULL" in ddl
    assert "digital_employee_profile_version VARCHAR(64)" in ddl
    assert "digital_employee_permissions_digest VARCHAR(160)" in ddl
    assert "CONSTRAINT uq_work_items_tenant_idempotency UNIQUE (tenant_id, idempotency_key)" in ddl
    assert "CONSTRAINT ck_work_items_status CHECK" in ddl
    assert "version INTEGER NOT NULL" in ddl


def test_postgresql_work_events_ddl_is_version_unique_and_foreign_keyed() -> None:
    ddl = str(CreateTable(work_events).compile(dialect=postgresql.dialect()))

    assert "CONSTRAINT uq_work_events_work_version UNIQUE (work_id, work_version)" in ddl
    assert "FOREIGN KEY(work_id) REFERENCES enterprise_work_items (work_id) ON DELETE RESTRICT" in ddl
    assert "CONSTRAINT ck_work_events_actor_type CHECK" in ddl
    assert "CONSTRAINT ck_work_events_from_status CHECK" in ddl
    assert "CONSTRAINT ck_work_events_to_status CHECK" in ddl


def test_postgresql_budget_usage_ddl_has_durable_counters() -> None:
    ddl = str(CreateTable(work_budget_usage).compile(dialect=postgresql.dialect()))

    assert "used_input_tokens BIGINT NOT NULL" in ddl
    assert "reserved_output_tokens BIGINT NOT NULL" in ddl
    assert "FOREIGN KEY(work_id) REFERENCES enterprise_work_items (work_id) ON DELETE RESTRICT" in ddl
    assert "CONSTRAINT ck_work_budget_usage_used_nonnegative CHECK" in ddl
    assert "CONSTRAINT ck_work_budget_usage_reserved_nonnegative CHECK" in ddl


def test_postgresql_budget_reservation_ddl_is_idempotent_and_bounded() -> None:
    ddl = str(CreateTable(work_budget_reservations).compile(dialect=postgresql.dialect()))

    assert "CONSTRAINT uq_work_budget_reservation_idempotency UNIQUE (work_id, idempotency_key)" in ddl
    assert "CONSTRAINT ck_work_budget_reservation_actual_within_reserved CHECK" in ddl
    assert "CONSTRAINT ck_work_budget_reservation_finalized CHECK" in ddl
    assert "FOREIGN KEY(work_id) REFERENCES enterprise_work_items (work_id) ON DELETE RESTRICT" in ddl


def test_postgresql_pack_snapshot_ddl_is_content_addressed_and_version_unique() -> None:
    ddl = str(CreateTable(pack_snapshots).compile(dialect=postgresql.dialect()))

    assert "pack_snapshot_id VARCHAR(160) NOT NULL" in ddl
    assert "asset_version_refs JSONB NOT NULL" in ddl
    assert "CONSTRAINT uq_pack_snapshots_version_digest UNIQUE (pack_id, pack_version, manifest_digest)" in ddl


def test_postgresql_work_contract_ddl_is_versioned_and_lifecycle_checked() -> None:
    ddl = str(CreateTable(work_contracts).compile(dialect=postgresql.dialect()))

    assert "PRIMARY KEY (work_id, contract_type, contract_version)" in ddl
    assert "payload JSONB NOT NULL" in ddl
    assert "FOREIGN KEY(work_id) REFERENCES enterprise_work_items (work_id) ON DELETE RESTRICT" in ddl
    assert "CONSTRAINT ck_work_contract_status CHECK" in ddl
    assert "CONSTRAINT ck_work_contract_confirmation_pair CHECK" in ddl
    assert "CONSTRAINT ck_work_contract_lifecycle CHECK" in ddl
