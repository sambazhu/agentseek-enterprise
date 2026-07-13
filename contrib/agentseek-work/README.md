# agentseek-work

`agentseek-work` provides reusable enterprise work-ledger primitives for AgentSeek.

Implemented M1 slices contain:

- immutable `WorkItem`, `WorkEvent`, and `WorkBudget` contracts;
- a deterministic, version-checked WorkItem state machine;
- terminal-state and transition validation;
- PostgreSQL-first SQLAlchemy schema and initial migration;
- tenant-scoped, idempotent WorkItem creation;
- optimistic WorkItem updates and append-only WorkEvent commits in one transaction;
- JSON round-trip validation before JSONB persistence;
- a Bub plugin entry point reserved for later runtime hooks.

The repository uses JSONB for `brief` and identifier snapshots on PostgreSQL.
SQLite is used only for portable transaction-semantics tests. A deployment must
apply the migration to PostgreSQL before wiring runtime hooks.

It does not contain WeCom protocol handling, employee identity lookup, report-writing rules,
file parsing, MCP configuration, or DOCX rendering. It does not yet provide worker
leases, recovery, approvals, outbox delivery, Profile loading, or PackSnapshot loading.
