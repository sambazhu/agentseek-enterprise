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
- priority-ordered queued claim with finite worker leases;
- lease renewal, graceful abandon, expired-lease recovery, and bounded retry failure;
- waiting-external wakeup and requester cancellation without holding a worker slot;
- durable model/token/external-query budget reservation, settlement, release, and crash forfeiture;
- exact-version `WorkPlaybook` registration without importing template-specific report code;
- one-phase-at-a-time claim, budget, execute, validate, and commit orchestration;
- fail-closed budget exhaustion to `waiting_approval`;
- a Bub plugin entry point reserved for later runtime hooks.

The repository uses JSONB for `brief` and identifier snapshots on PostgreSQL.
SQLite is used only for portable transaction-semantics tests. A deployment must
apply schema revision 2 to PostgreSQL before wiring runtime hooks.

`PhaseWorker` reserves the phase's declared maximum resource use before the
playbook can call a model or external service. It settles actual aggregate use
before committing the phase transition. An active reservation whose worker dies
is charged at its reserved ceiling when the lease is recovered. This deliberately
prefers conservative accounting over allowing retries to bypass a budget.

The template owns concrete playbooks. It registers an exact `(playbook_id,
version)` implementation through `WorkPlaybookRegistry`. `agentseek-work` does
not import the enterprise WeCom template or report-writing modules.

It does not contain WeCom protocol handling, employee identity lookup, report-writing rules,
file parsing, MCP configuration, or DOCX rendering. It does not yet provide budget
extensions, approval decisions, outbox delivery, Profile loading, PackSnapshot
loading, a production scheduler channel, or a concrete report playbook.
