---
title: Enterprise WeCom v0.1.2 GA readiness
type: reference
audience: [A3, A4]
runs: no
verified_on: 2026-09-02
sources:
  - CHANGELOG.md
  - PRODUCTION_FREEZE.md
  - ROADMAP.md
  - V0.1.2_M0_1_WECOM_PROTOCOL_BASELINE.md
  - V0.1.2_M0_2_WECOM_TRANSPORT_VERIFICATION_RECORD.md
  - V0.1.2_M0_3_WECOM_DURABLE_VERIFICATION_RECORD.md
  - V0.1.2_M0_4_WECOM_CALLBACK_VERIFICATION_RECORD.md
  - V0.1.2_M0_5_WECOM_LONG_CONNECTION_VERIFICATION_RECORD.md
  - V0.1.2_M0_6_WECOM_APPLICATION_VERIFICATION_RECORD.md
---

# Enterprise WeCom v0.1.2 GA readiness

## Decision

| Field | Value |
| --- | --- |
| Result | PASS |
| Release scope | WeCom Transport Foundation |
| Production verification record | `04c4b4a70baa2a04ae612e6416553b3f92c239f5` |
| Final verified runtime code | `50b91881c9a09d303765cb57b15a0328d73c87eb` |
| Release tag | `enterprise-wecom-v0.1.2-ga` |
| Previous rollback tag | `enterprise-wecom-v0.1.1-ga` |
| Core-tag collision rule | Do not create or move upstream tag `v0.1.2` |

## Release gates

| Gate | Result | Evidence |
| --- | --- | --- |
| M0.1 protocol baseline | PASS | Direct/group isolation, quote semantics, identity hydration, secret redaction |
| M0.2 transport kernel | PASS | Callback address and live compatibility verification |
| M0.3 durable messaging | PASS | Deterministic tests plus live graceful and periodic recovery |
| M0.4 Callback hardening | PASS | Live media ordering, first acknowledgement, card interaction, restart recovery |
| M0.5 long connection | PASS | Live stream, proactive message/card, group isolation, native-exit recovery, reconciliation |
| M0.6 application transport | PASS | Live inbound, member/department/tag/file delivery, subtree authorization, SIGTERM recovery |
| Git history | PASS | M0.1–M0.6 production integration is fast-forward only; historical BLOCKED records remain |
| GitLab/GitHub branch mirrors | PASS | Both `production` refs resolved to `04c4b4a` before the release-metadata commit |
| Sensitive artifacts | PASS | No `.env`, credentials, logs, runtime, media, or SQLite files in the release diff |

## Deterministic gate baseline

| Suite | Final verified result |
| --- | --- |
| `agentseek-wecom` | 154 passed |
| M0.6 application + outbound | 36 passed |
| Enterprise digital employee example | 335 passed, 1 skipped |
| `agentseek-enterprise` | 88 passed |
| `agentseek-contextseek` | 47 passed, 1 skipped |
| Enterprise WeCom template rendering | 5 passed |
| WeCom integration | 3 passed |
| Example production checks | 13 passed |
| Ruff, ty, docs-test, lock, shell, diff | PASS |

Counts record the final M0.6 development baseline. Release metadata changes require
documentation, template, release-consistency, and diff gates; they do not require a
repeat of live WeCom Oracles because runtime code is unchanged.

## Delivered scope

| Capability | Included |
| --- | --- |
| AI Bot Callback compatibility and hardening | Yes |
| AI Bot long connection, stream, and qualified proactive delivery | Yes |
| Common self-built application callback and explicit proactive delivery | Yes |
| Encrypted single-host durable inbox/outbox and msgid deduplication | Yes |
| Graceful shutdown, lease release, periodic recovery, and manual failed-inbox reconciliation | Yes |
| Group short-term and semantic isolation | Yes |
| Prompt-safe separation of routing metadata | Yes |
| Department-root recursive visibility with fail-closed validation | Yes |

## Deferred scope

| Capability | Target |
| --- | --- |
| Organization authorization and Identity Provider abstraction | v0.1.3 M1 |
| Reusable Playbook and Capability extension SDK | v0.1.3 M2 |
| Information Technology digital employee and requirement-review Playbook | v0.1.3 M3 |
| Multi-host durable coordination and readiness/metrics | v0.1.3 M4 |
| Browser SSO, claim review, archive, and task governance | v0.1.3 M5 |

## Publication contract

| Check | Required result |
| --- | --- |
| Production ancestry | Previous `production` is an ancestor of the release commit |
| Merge commits | None in the release-metadata range |
| Branch publication | GitLab and GitHub `production` resolve to one commit |
| Tag publication | GitLab and GitHub `enterprise-wecom-v0.1.2-ga` resolve to the same annotated tag target |
| Tag immutability | No force push, deletion, or retargeting |
| Runtime impact | No restart, schema migration, or environment change for metadata-only closure |

## Rollback contract

| Action | Reference |
| --- | --- |
| Restore previous immutable release | `enterprise-wecom-v0.1.1-ga` |
| Restore Callback-only robot mode | Select Callback in WeCom and stop the long-connection instance |
| Stop application notifications | Disable the application Transport without changing the AI Bot |
| Preserve audit state | Keep durable SQLite, blocked outbox, logs, and historical verification records |
