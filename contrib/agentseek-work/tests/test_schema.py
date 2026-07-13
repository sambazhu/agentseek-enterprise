from agentseek_work.schema import work_events, work_items
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable


def test_postgresql_work_items_ddl_uses_jsonb_and_idempotency_constraint() -> None:
    ddl = str(CreateTable(work_items).compile(dialect=postgresql.dialect()))

    assert "brief JSONB NOT NULL" in ddl
    assert "skill_digests JSONB NOT NULL" in ddl
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
