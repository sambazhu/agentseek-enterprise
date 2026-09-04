---
title: Enterprise WeCom Digital Employee Architecture and Deployment Boundaries
type: explanation
audience: [A2, A3, A4]
runs: no
verified_on: 2026-09-04
sources:
  - README.md
  - docs/assets/enterprise-wecom/enterprise-wecom-v0.1.2-architecture.svg
  - contrib/agentseek-wecom/src/agentseek_wecom/addressing.py
  - contrib/agentseek-wecom/src/agentseek_wecom/channel.py
  - contrib/agentseek-wecom/src/agentseek_wecom/config.py
  - contrib/agentseek-contextseek/src/agentseek_contextseek/plugin.py
  - contrib/agentseek-enterprise/src/agentseek_enterprise/runtime.py
  - contrib/agentseek-work/src/agentseek_work/models.py
  - examples/enterprise_wecom_digital_employee/DEVELOPER_GUIDE.md
  - examples/enterprise_wecom_digital_employee/ROADMAP.md
  - examples/enterprise_wecom_digital_employee/PRODUCTION_FREEZE.md
  - examples/enterprise_wecom_digital_employee/src/enterprise_wecom_digital_employee/agent.py
  - examples/enterprise_wecom_digital_employee/src/enterprise_wecom_digital_employee/playbook_registry.py
  - examples/enterprise_wecom_digital_employee/src/enterprise_wecom_digital_employee/work_composition.py
  - examples/enterprise_wecom_digital_employee/digital_employees/industry-report/profile.yaml
  - examples/enterprise_wecom_digital_employee/digital_employees/industry-report/pack.yaml
---

# Enterprise WeCom Digital Employee Architecture and Deployment Boundaries

> **Summary:** One logical deployment unit represents one digital employee.
> That employee owns one Profile, one capability pool, and zero or more
> Playbooks within the same role boundary. Teams create independent deployments
> from the shared scaffold instead of mixing several employees in one runtime.

## Context

`enterprise-wecom` combines a reusable runtime kernel with an instantiable
digital employee example. The kernel owns WeCom protocols, identity, files,
memory, Work, and lifecycle behavior. Each product team owns its employee's
role, capabilities, workflows, knowledge, and deployment configuration.

A digital employee is a stable business identity. It is neither a human WeCom
user nor a Playbook. Its `digital_employee_id` can serve many people and group
chats.

![Enterprise WeCom v0.1.2 architecture](../assets/enterprise-wecom/enterprise-wecom-v0.1.2-architecture.svg)

- [Open the scalable SVG](../assets/enterprise-wecom/enterprise-wecom-v0.1.2-architecture.svg)
- [Download the 4096 × 2880 PNG](../assets/enterprise-wecom/enterprise-wecom-v0.1.2-architecture-4k.png)

## Core concepts

| Concept | Meaning | Relationship |
| --- | --- | --- |
| Framework and scaffold | Shared AgentSeek, Bub, and `contrib` capabilities | One framework creates many deployment units |
| Logical deployment unit | One independently configured, released, monitored, and rolled-back employee | One unit has one `digital_employee_id` |
| Digital employee | Stable role and business identity serving people, groups, and services | One employee has one active Profile |
| Profile / Job Charter | Role, responsibilities, service catalog, and authorization ceiling | One Profile can declare many services |
| Pack | Versioned Skills, Playbooks, Policies, and Assets used by a Profile | One deployment loads one active Pack version |
| Capability Registry | Shared capability pool for direct assistance and formal workflows | One effective pool per employee |
| Playbook | Formal service with tasks, contracts, state, checkpoints, or approval | Zero or more per employee |
| AI Bot | Primary conversational WeCom entry | Callback or long connection, never both for one Bot |
| Self-built application | Supplementary member, department, tag, card, and file channel | Can run beside either Bot mode |

## Why one deployment represents one employee

Different employees normally have different owners, organization authorization,
department knowledge, MCP permissions, business ledgers, and approval duties.
Combining them expands the impact of a routing or configuration error.

```text
Shared framework and scaffold
├── Team A customization → Strategy employee → Independent deployment unit
├── Team B customization → IT employee       → Independent deployment unit
└── Team C customization → Another employee  → Independent deployment unit
```

“Deployment unit” is more precise than “process.” v0.1.2 verifies one process
per unit on one host. Future replicas of the same employee may share one
`digital_employee_id`, Profile, Pack, and consistency boundary. They do not
become additional employees.

The current [`build_spec()`](https://github.com/sambazhu/agentseek-enterprise/blob/production/examples/enterprise_wecom_digital_employee/src/enterprise_wecom_digital_employee/agent.py#L237)
constructs one employee Registry and Runnable. It does not dynamically select
another Profile from Bot ID, department, or message content.

## How one employee owns multiple Playbooks

The Profile service catalog and Pack `playbooks` are collections.
[`RoutedAgentRunnable`](https://github.com/sambazhu/agentseek-enterprise/blob/production/examples/enterprise_wecom_digital_employee/src/enterprise_wecom_digital_employee/agent.py#L128)
builds a bound Agent for every registered `playbook_ref`. Multiple Playbooks are
therefore part of the current architecture, not a reason to create a
multi-employee runtime.

```text
One digital employee
├── One Profile / Job Charter
├── One Capability Registry
└── Zero or more formal Playbooks
    ├── Playbook A
    ├── Playbook B
    └── Playbook C
```

Playbooks owned by one employee must share the same role, owner, authorization
ceiling, and effective capability pool. Each still keeps its own
`playbook_ref`, routing contract, WorkItems, state, approvals, and version.
Ambiguous routing asks the person to clarify. An existing task continues its
current binding first.

Split a service into another digital employee when its department identity,
data authorization, approval owner, or role mission differs.

## Keep Transport separate from business identity

[`WeComChannel`](https://github.com/sambazhu/agentseek-enterprise/blob/production/contrib/agentseek-wecom/src/agentseek_wecom/channel.py#L167)
selects Callback or long connection for one deployment's AI Bot. It can also
mount the supplementary application Transport. Bot ID and Agent ID identify a
channel. `digital_employee_id` identifies the business employee.

```text
AI Bot ── Callback or long connection ──> Digital employee
                                             |
Shared application ── notifications/files ──+
```

Replacing a Bot, switching Transport, or migrating an application must not
change the employee's business identity or orphan its WorkItems.

Several employees may share one application's outbound capability only when
every message preserves its source employee, idempotency key, visibility, and
audit context. A shared application has one inbound callback. v0.1.2 does not
provide a central router that dispatches this callback among several employee
deployments, so several instances must not consume it concurrently.

## Conversation, memory, and data isolation

[`ConversationAddress`](https://github.com/sambazhu/agentseek-enterprise/blob/production/contrib/agentseek-wecom/src/agentseek_wecom/addressing.py#L16)
records tenant, Bot or Agent, Transport, conversation, sender, and reply
deadline. Group sessions contain Bot ID and chat ID. The compatible direct-chat
session remains `wecom:<userid>`.

The direct-chat string alone cannot isolate two digital employees. Until every
storage key includes `digital_employee_id`, each deployment needs a separate
database, schema, table prefix, or physical storage path. Two employees must
not directly share the current short-term-memory table.

The target logical scope is:

```text
tenant_id
+ digital_employee_id
+ conversation_id
```

Deployments may share a PostgreSQL cluster, object store, or model service while
keeping these logical boundaries:

| Data | Isolation requirement |
| --- | --- |
| WeCom inbox/outbox | Separate durable path; replicas first require shared durable state and distributed leases |
| Short-term memory | Separate database/schema/table, or a key containing `digital_employee_id` |
| Semantic memory | Separate backend/table, or retrieval scoped by tenant, employee, person, and group conversation |
| Explicit durable memory | Separate Store, or namespace scoped by tenant, employee, and person |
| Work ledger | Every task retains `digital_employee_id`, Profile, and PackSnapshot |
| Files and Artifacts | Organize by tenant, employee, person, conversation, or WorkItem |
| Logs and audit | Use a distinct service identity, release version, and protected directory; omit credentials and signed URLs |

Memory helps interpret context. It does not prove authorization or completion.
The Work ledger remains authoritative for formal tasks, contracts, approvals,
releases, and delivery state.

## Ownership boundaries

| Owner | Maintains | Does not privately fork |
| --- | --- | --- |
| Framework maintainers | AgentSeek lifecycle, Bub contracts, WeCom Transport, common identity/memory/file/Work kernels | Department-specific role rules |
| Employee product team | Profile, Pack, Skills, Playbooks, capability mapping, business tests, and department knowledge | Long-lived copies of common Transport or durable code |
| Operations | `.env`, credentials, ports, storage, processes, health, logs, backups, and rollback | Host configuration or real secrets in Git |
| Application and data owner | Application visibility, MCP access, knowledge data, and approval responsibility | Display names as authorization identifiers |

Teams release their employee independently and continue to consume the shared
framework GA. Business differences use extension points instead of copied
`contrib` implementations.

## Required deployment decisions

Every deployment defines:

- one stable `digital_employee_id`, Profile version, and Pack version;
- one AI Bot and exactly one of Callback or long connection;
- whether it uses a shared application and which sources and targets are valid;
- separate ports, runtime, durable, memory, Work, file, and audit boundaries;
- real authorization for knowledge, MCP, models, and external retrieval;
- process management, health, backups, credential rotation, and rollback;
- acceptance evidence for direct chats, multiple people, two groups, media,
  restart recovery, and sensitive-data scans.

## Current v0.1.2 boundary

v0.1.2 verifies one digital employee deployment in one process on one host. It
does not deliver:

- multi-host or multi-replica coordination for one logical employee;
- several employees sharing one Gateway with dynamic Profile selection;
- central routing of one application's inbound callback to several employees;
- shared current memory tables without explicit logical isolation.

These boundaries do not prevent one department from deploying several digital
employees or one employee from owning several Playbooks. They preserve each
employee's business identity and deployment boundary.

## Related documentation

- [Enterprise WeCom template](enterprise-wecom-template.md)
- [Digital employee developer guide](https://github.com/sambazhu/agentseek-enterprise/blob/production/examples/enterprise_wecom_digital_employee/DEVELOPER_GUIDE.md)
- [Deployment quickstart](https://github.com/sambazhu/agentseek-enterprise/blob/production/examples/enterprise_wecom_digital_employee/DEVELOPER_QUICKSTART.md)
- [Extending Skills, MCP, and Playbooks](https://github.com/sambazhu/agentseek-enterprise/blob/production/examples/enterprise_wecom_digital_employee/EXTENDING_DIGITAL_EMPLOYEE.md)
- [Development workflow](https://github.com/sambazhu/agentseek-enterprise/blob/production/examples/enterprise_wecom_digital_employee/DEVELOPMENT_WORKFLOW.md)
- [v0.1.2 production freeze](https://github.com/sambazhu/agentseek-enterprise/blob/production/examples/enterprise_wecom_digital_employee/PRODUCTION_FREEZE.md)
- [Evolution roadmap](https://github.com/sambazhu/agentseek-enterprise/blob/production/examples/enterprise_wecom_digital_employee/ROADMAP.md)
