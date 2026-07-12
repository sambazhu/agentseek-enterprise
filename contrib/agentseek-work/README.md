# agentseek-work

`agentseek-work` provides reusable enterprise work-ledger primitives for AgentSeek.

The first M1 slice contains:

- immutable `WorkItem`, `WorkEvent`, and `WorkBudget` contracts;
- a deterministic, version-checked WorkItem state machine;
- terminal-state and transition validation;
- a Bub plugin entry point reserved for later repository and runtime hooks.

It does not contain WeCom protocol handling, employee identity lookup, report-writing rules,
file parsing, MCP configuration, or DOCX rendering.

