---
title: Enterprise WeCom v0.1.2 production freeze
type: reference
audience: [A3, A4]
runs: no
verified_on: 2026-09-02
sources:
  - CHANGELOG.md
  - ROADMAP.md
  - V0.1.2_M0_1_WECOM_PROTOCOL_BASELINE.md
  - V0.1.2_M0_2_WECOM_TRANSPORT_VERIFICATION_RECORD.md
  - V0.1.2_M0_3_WECOM_DURABLE_VERIFICATION_RECORD.md
  - V0.1.2_M0_4_WECOM_CALLBACK_VERIFICATION_RECORD.md
  - V0.1.2_M0_5_WECOM_LONG_CONNECTION_VERIFICATION_RECORD.md
  - V0.1.2_M0_6_WECOM_APPLICATION_VERIFICATION_RECORD.md
  - ../../contrib/agentseek-wecom/src/agentseek_wecom/channel.py
  - ../../contrib/agentseek-wecom/src/agentseek_wecom/durable.py
  - ../../contrib/agentseek-wecom/src/agentseek_wecom/transports/application.py
  - ../../contrib/agentseek-wecom/src/agentseek_wecom/transports/long_connection.py
---

# Enterprise WeCom v0.1.2 production freeze

## Baseline

| Ref or artifact | Value | Purpose |
| --- | --- | --- |
| Production branch | `production` | Latest verified enterprise deployment baseline |
| GA tag | `enterprise-wecom-v0.1.2-ga` | Immutable v0.1.2 source and rollback reference |
| Verified runtime code | `50b91881c9a09d303765cb57b15a0328d73c87eb` | Final department-subtree implementation |
| Linux PASS record | `04c4b4a70baa2a04ae612e6416553b3f92c239f5` | M0.6 final live verification and M0.1–M0.6 closure |
| Previous GA tag | `enterprise-wecom-v0.1.1-ga` | Immutable rollback baseline |
| Previous GA commit | `d61c5316c0846e0d06d80e8666d0bd1609057144` | Previous production runtime |
| Verification host | Linux `agent01-hh` | Isolated live Callback, long-connection, and application verification |
| Verification date | 2026-09-02 | Final M0.6 PASS date |

The GA tag belongs to the Enterprise WeCom release line. The upstream AgentSeek
Core tag named `v0.1.2` is a different release and must not be moved or reused.

## Frozen capabilities

| Area | Frozen behavior |
| --- | --- |
| AI Bot Callback | Compatible encrypted callback, fast acknowledgement, six-minute stream, one-hour response URL, media queueing, quotes, and template-card events |
| AI Bot long connection | WebSocket ownership, heartbeat, reconnect, active stream, 24-hour qualified proactive Markdown/Card, and explicit-rejection fallback |
| Self-built application | Independent encrypted callback and explicit member, authorized department subtree, tag, text, media, news, Markdown, and template-card delivery |
| Conversation address | Tenant, Bot/Agent, transport, chat type, stable conversation, sender identity, interaction time, and reply deadline remain separate fields |
| Direct chat | `wecom:<userid>` remains compatible for one digital employee per deployment |
| Group chat | Bot and group conversation boundaries remain stable; short-term and semantic memory cannot cross groups |
| Durable messaging | Encrypted single-host SQLite inbox/outbox, persisted msgid deduplication, leases, reply deadlines, graceful release, periodic recovery, and bounded manual reconciliation |
| Prompt projection | Routing IDs, response capabilities, signed URLs, and private identity fields remain outside model-visible context |
| Application visibility | Empty dimensions delegate final authorization to WeCom; non-empty department roots resolve through the token-scoped recursive subtree and malformed results fail closed |

## Verification record

| Slice | Result | Evidence |
| --- | --- | --- |
| M0.1 protocol and secret baseline | PASS | Group/direct isolation, quotes, plaintext userid compatibility, settings redaction |
| M0.2 transport kernel | PASS | Callback compatibility and stable cross-version addresses |
| M0.3 durable messaging | PASS | Inbox/outbox terminal state, SIGTERM lease release, immediate and periodic recovery |
| M0.4 Callback hardening | PASS | First acknowledgement before media I/O, session order, card-event recovery |
| M0.5 long connection | PASS | Stream, proactive delivery, group isolation, native crash recovery, failed-inbox reconciliation |
| M0.6 application transport | PASS | Inbound callback, member/department/tag/file delivery, department subtree, restart recovery |

Historical BLOCKED sections remain in the verification files. They are part of the
audit trail and document the fixes that produced the final PASS.

## Deployment topology

```text
AI Bot A -> Callback OR long connection -> digital employee A
AI Bot B -> Callback OR long connection -> digital employee B
                         |
                         `-> optional common self-built application
                             -> explicit member/department/tag notification
```

One AI Bot selects exactly one robot transport. The application is supplementary;
it is not a third robot mode and does not provide streamed responses.

## Release boundaries

| Boundary | v0.1.2 status |
| --- | --- |
| Multi-Profile gateway | Not delivered. One deployment still loads one Profile and one digital employee. |
| Second department digital employee | Deferred to v0.1.3 with organization authorization and the extension SDK. |
| Multi-host durable store | Not delivered. The verified durable database is local encrypted SQLite. |
| Application Work routing | Not delivered. Work composition cannot infer an application recipient; `direct_file` remains disabled. |
| Browser SSO | Not delivered. Signed-link identity hardening remains later work. |
| Local framework logs | Restricted logs can contain internal `wecom:<userid>` session ids and reply text. External structured events remain hashed or redacted. |

## Mirrors

| Location | Required ref |
| --- | --- |
| GitHub | `https://github.com/sambazhu/agentseek-enterprise` |
| Company GitLab | `http://172.200.6.12:9091/harness_agent/agentseek-enterprise` |
| Branch | Both `production` refs resolve to the GA commit |
| Tag | Both `enterprise-wecom-v0.1.2-ga` refs resolve to the same immutable tag target |

## Rollback

| Scenario | Rollback reference | Data action |
| --- | --- | --- |
| Full release rollback | `enterprise-wecom-v0.1.1-ga` | No schema rollback; preserve runtime databases and logs |
| Disable long connection | Select Callback for that AI Bot and stop the long-connection instance | Preserve durable SQLite for audit |
| Disable common application | Disable application Transport and retain the AI Bot transport | Preserve blocked/delivered outbox history |

Do not commit `.env`, credentials, logs, media, runtime directories, SQLite files,
model files, or host-specific paths. GA tags are immutable; later fixes require a
new commit and release tag.
