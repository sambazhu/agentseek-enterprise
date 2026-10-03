---
title: Execution Isolation and Sandboxes
type: explanation
audience: [A2, A3, A4]
runs: no
verified_on: 2026-09-06
sources:
  - docs/concepts/enterprise-wecom-architecture.md
  - examples/enterprise_wecom_digital_employee/ROADMAP.md
  - examples/enterprise_wecom_digital_employee/V0.1.3_EXECUTION_ISOLATION_PLAN.md
---

# Execution Isolation and Sandboxes

Execution isolation limits the files, network, and resources available to one Skill, Tool,
or Playbook action and separates execution failures from the Gateway.
The v0.1.3 proposal selects CubeSandbox as its only production backend.
It remains an architecture proposal, not an implemented or deployed feature.

## Deployment, data, and execution boundaries

One logical deployment represents one digital employee, serving multiple requesters and Playbooks.
Execution isolation adds task-scoped microVMs:

```text
Gateway / Enterprise Main Agent
  -> Execution Broker (authorization, leases, import/export, reconciliation)
       -> CubeSandbox: one exclusive VM per execution attempt
       -> Execution Ledger + Content Store
```

The Main Agent retains identity, memory, WorkItems, and message delivery.
A dedicated Tool or execution sub-agent gets sandbox access; the Main Agent does not receive
a general shell by default. DeepAgents supplies the filesystem/command interface;
the Broker enforces enterprise authorization and lifecycle.

Cube can run on separate private-network KVM nodes while Gateways remain on ordinary VMs.
A shared cluster requires verified resource authorization for each service identity.
Shared infrastructure does not authorize shared business data.

## VM scope and working state

An execution attempt can contain multiple commands: write a script, execute, correct, and export.
Release the VM at phase completion or approval wait. A later phase starts a new VM from a
committed file revision. Ordinary Turn file tasks have a task_id without requiring a WorkItem.

Business data has a longer lifetime than the VM:

| Data | Scope | Representation |
| --- | --- | --- |
| Skills/templates | Digital employee + release | Immutable, read-only assets |
| Background material/attachments | Authorized file manifest + version | Read-only /context and /inputs |
| Working copy | Task/work + revision + attempt | Single-writer /workspace |
| Candidate output | Execution + attempt | /outputs, validated before commit |
| Formal Artifact | WorkItem + contract/approval version | Existing publication and delivery ledgers |
| Memory/database | Existing enterprise and conversation scope | Enterprise storage, not mounted in the VM |

Initially, import and export small files explicitly. Large data packages may use sealed,
versioned Cube read-only Volumes. A read-only attachment is not an immutable snapshot:
other writers can still change the volume. Published packages need separate write protection
and fixed digests. Do not mount an entire department or employee directory read-write.

## Persistence and recovery

Scripts compute on temporary guest storage. The Broker collects outputs, checks paths, size,
type, and digests, saves content to a private S3-compatible Content Store, then publishes a
WorkspaceRevision in the ledger. ExecutionOutput holds drafts; formal Work Artifacts retain
their source-contract and approval requirements.

Recovery starts from the last successfully committed revision. A zero exit code does not prove
durable output. Unknown external side effects require reconciliation rather than blind retries.
VM snapshot recovery is disabled initially; snapshots do not replace content or business ledgers.

## Selection conditions

Pin and validate the Cube SDK, server, template, and Volume plugin versions.
Explicit authentication, denied-by-default egress, quotas, and resource isolation are required.
MicroVMs do not automatically enforce enterprise authorization or prevent credential exfiltration.
Other runtimes remain research comparisons, not parallel v0.1.3 implementations.

## Related documentation

- [Enterprise WeCom architecture](enterprise-wecom-architecture.md)
- [v0.1.3 architecture review and implementation plan](https://github.com/sambazhu/agentseek-enterprise/blob/production/examples/enterprise_wecom_digital_employee/V0.1.3_EXECUTION_ISOLATION_PLAN.md)
- [Evolution roadmap](https://github.com/sambazhu/agentseek-enterprise/blob/production/examples/enterprise_wecom_digital_employee/ROADMAP.md)
