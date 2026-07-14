# agentseek-work

`agentseek-work` provides reusable enterprise work-ledger primitives for AgentSeek.

Implemented M1 and M2-01 slices contain:

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
- immutable, content-addressed `PackSnapshot` metadata and WorkItem binding validation;
- deterministic `DirectTurn`/`WorkItem` routing from server-owned tool contracts;
- an opt-in Bub message-key/state-enrichment hook with an explicit template binding boundary.
- generic, immutable `WorkContractSnapshot` versions with provisional, confirmed,
  and superseded lifecycle states;
- requester-only, expected-version contract confirmation and atomic contract revision;
- one current version per WorkItem contract type, enforced by a partial unique index.
- immutable, tenant-scoped `SourceRecord` provenance with locator/query/result
  digests, license and snapshot status, plus idempotent repository operations.

The repository uses JSONB for `brief` and identifier snapshots on PostgreSQL.
SQLite is used only for portable transaction-semantics tests. A deployment must
apply schema revision 7 to PostgreSQL before enabling M2 source persistence.

`PhaseWorker` reserves the phase's declared maximum resource use before the
playbook can call a model or external service. It settles actual aggregate use
before committing the phase transition. An active reservation whose worker dies
is charged at its reserved ceiling when the lease is recovered. This deliberately
prefers conservative accounting over allowing retries to bypass a budget.

The template owns concrete playbooks. It registers an exact `(playbook_id,
version)` implementation through `WorkPlaybookRegistry`. `agentseek-work` does
not import the enterprise WeCom template or report-writing modules.

It does not contain WeCom protocol handling, employee identity lookup, report-writing rules,
file parsing, MCP configuration, or DOCX rendering. Contract confirmation is not the later
general approval ledger. The package does not yet provide approval decisions, outbox delivery,
Profile loading, pack content storage, a production scheduler
channel, or a concrete report playbook. Pack content loading, Profile authorization,
Profile-scoped Skill materialization, and WorkItem factory policy belong to the template;
this package persists generic ledger/snapshot metadata, validates bindings, and enforces
server-owned routing contracts.
