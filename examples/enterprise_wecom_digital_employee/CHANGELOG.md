---
title: Enterprise WeCom changelog
type: reference
audience: [A3, A4]
runs: no
verified_on: 2026-09-01
sources:
  - contrib/agentseek-files/src/agentseek_files
  - contrib/agentseek-wecom/src/agentseek_wecom
  - contrib/agentseek-contextseek/src/agentseek_contextseek/plugin.py
  - examples/enterprise_wecom_digital_employee/DEPLOYMENT_NOTES.md
  - examples/enterprise_wecom_digital_employee/PRODUCTION_FREEZE.md
  - examples/enterprise_wecom_digital_employee/V0.1.2_M0_5_WECOM_LONG_CONNECTION.md
---

# Enterprise WeCom changelog

## v0.1.2 M0.5 — pending Linux prompt-projection re-verification

Status — Feature branch only. Transport live Oracles passed, and ContextSeek group
isolation is zero-crossing in fresh groups. Linux evidence showed that the apparent
hexadecimal hallucinations were inbound `msgid` values exposed to the model.
Prompt-projection fix `5668e2f` requires the targeted Linux group C/D Oracle. Do
not merge to `production` before that Oracle passes.

### Added

| Area | Change |
| --- | --- |
| AI Bot long connection | WebSocket subscribe, documented JSON heartbeat, reconnect, and local single-owner lock. |
| Active stream reply | Reuse inbound `req_id` and one `stream.id`; deliver the first acknowledgement before Agent work. |
| Reply recovery | Persist terminal long-connection streams in the encrypted outbox with a 24-hour deadline. |
| Proactive delivery | Send idempotent Markdown or template-card messages to previously observed direct or group sessions. |
| Card-event terminal | Route card-click Agent results through idempotent proactive Markdown without using a message-stream reply command. |
| Durable qualification | Local SQLite revision 2 adds encrypted proactive-conversation qualification. |
| Operations | Preserve `:12000/health`; report selected transport and subscription readiness. |
| Live Oracle | Default-off trigger sends proactive Markdown and a button card for end-to-end verification. |

### Fixed

| Problem | Resolution |
| --- | --- |
| One employee's semantic recall could cross WeCom groups | Keep direct chat at employee scope; append the anonymous enterprise session key for group retrieval and storage. |
| Group runtime missing a trusted conversation key | Fail closed instead of falling back to the broader employee scope. |
| A model treated an employee-provided marker as a technical identifier | Label historical user source separately, copy requested literals exactly, and forbid fabricated UUID/hash/internal IDs. |
| A prior assistant hallucination competed with stored user text | Mark historical assistant output as fallible and prefer conflicting historical user source. |
| WeCom routing identifiers could be interpreted as business content | Keep rich routing metadata in private channel context, but project only semantic message fields into the model prompt. |
| Work idempotency previously read `msgid` from model-visible raw data | Read the private internal message ID first while retaining a legacy fallback for stored messages. |

### Boundaries

| Boundary | Behavior |
| --- | --- |
| Callback coexistence | Configuration supports either transport; the WeCom console permits only one mode for one robot. |
| Direct file | Disabled. Official chunked media upload is not implemented in M0.5. |
| Arbitrary recipient | Not supported by AI Bot. M0.6 self-built application transport owns this capability. |
| Shared PostgreSQL | No schema or data change. The revision 1→2 migration applies only to the dedicated local durable SQLite. |
| Digital-employee binding | M0.5 remains one deployment, one Profile, and one digital employee; the new long bot is a Transport canary for `industry-report`. |
| Short-term memory | `wecom:<userid>` remains compatible for one digital employee. A future shared multi-employee keyspace must add `digital_employee_id`. |
| Semantic memory | Direct chat remains employee-scoped. Group chat adds an anonymous conversation scope and cannot retrieve another group's turns. |
| Common application | `CORP_ID`/`APP_SECRET` do not constitute `WeComAppTransport`; shared member/department/tag delivery remains M0.6. |

## enterprise-wecom-v0.0.9-ga — 2026-07-12

Status — Final production GA. The audited runtime commit is
`8128aac4c37a46264477709adf07bd99e5eadb58`. The immutable rollback baseline is
`enterprise-wecom-v0.0.8-ga`.

### Added

| Area | Change |
| --- | --- |
| WeCom AI Bot media | Receive signed-URL `file`, `image`, `video`, `voice`, and mixed messages. |
| Encrypted downloads | Decrypt downloaded AI Bot media with the WeCom EncodingAESKey. |
| Scoped storage | Store inbound files under HMAC-scoped tenant, employee, session, and date directories. |
| Local extraction | Parse `.txt`, `.md`, `.csv`, and `.json` without a remote extractor. |
| MinerU extraction | Parse PDF, DOCX, XLSX, PPTX, and supported image formats through MinerU. |
| OCR selection | Run non-OCR extraction first, then retry scanned documents with OCR. |
| Mixed documents | Return digital text immediately and run background OCR for unresolved image references. |
| CurrentFiles | Refresh completed extraction state on later turns and expose bounded model context. |
| Large-file analysis | Analyze complete extracted text without placing the full document in the model prompt. |
| Spreadsheet analysis | Detect real headers, aggregate multiple sheets, retain sheet names, and report per-sheet statistics. |
| Background OCR diagnostics | Record `replace`, `append_supplement`, `unchanged`, and image-reference counts in metadata. |

### Fixed

| Problem | Resolution |
| --- | --- |
| AI Bot media treated as application `media_id` | Download the signed callback URL directly. |
| Missing PDF extension | Infer extensions from Content-Type, magic bytes, and MIME fallback. |
| AsyncClient used with synchronous request content | Send async-compatible bytes. |
| MinerU result remained pending in later turns | Re-poll stored records and rebuild CurrentFiles context. |
| Digital PDFs always used slow OCR | Use automatic non-OCR-first extraction. |
| Scanned or mixed pages lost text | Retry with OCR and persist the improved background result. |
| OCR image text appeared unreadable to the model | Mark parsed and unparsed image references explicitly. |
| Large spreadsheets were limited to the first excerpt | Add the read-only `analyze_file` tool. |
| Merged spreadsheet title rows became headers | Detect the first structural header row. |
| Multi-sheet grouping stopped after the first sheet | Aggregate all matching tables and merge per-group name sets. |
| Sheet-specific questions returned another sheet | Associate MinerU H1 headings with tables and filter requested sheets. |
| Unchanged background OCR overwrote useful output | Preserve the first pass and record `merge_strategy=unchanged`. |

### Verification

| Suite or live path | Result |
| --- | --- |
| `agentseek-files` | 47 passed |
| `agentseek-wecom` | 38 passed |
| `agentseek-enterprise` | 79 passed |
| Cookiecutter template rendering | 25 passed |
| Identity, short-term memory, durable memory, pgvector, MCP, Langfuse | No regression |
| PDF: digital, scanned, mixed | Passed |
| DOCX: text, table, image OCR | Passed |
| XLSX: multiple sheets, large-file statistics, embedded image | Passed |
| PPTX: six ordered slides, table, chart text, logo | Passed |
| TXT, image, voice, mixed message | Passed |
| Gateway health | 0 SIGBUS, 0 traceback, 0 non-200 callback responses in final validation |
| MCP frozen files against `68d7b25` | Zero diff |

### Known limitations

| Limitation | Behavior |
| --- | --- |
| MinerU returns no OCR text for an embedded image | Preserve the first pass and record `unchanged`; other sheets remain available. |
| Non-text charts, logos, signatures, and seals | Keep the image reference unparsed and do not infer its contents. |
| Short OCR captions below the context threshold | Text may be present in the extract while the image remains marked unparsed. This does not block GA. |
| AI Bot proactive notification | Not available; the employee asks again and CurrentFiles refreshes the completed result. |
| Langfuse runtime top-level release badge | Runtime metadata contains the release, but the UI top-level badge may be empty. |
| pgvector multi-source recall latency | A complex structured answer may take tens of seconds. |

## enterprise-wecom-v0.0.8-ga — 2026-07-08

Status — Previous production GA and current rollback baseline.

| Area | Baseline capability |
| --- | --- |
| Runtime | Enterprise identity, short-term memory, durable memory, pgvector semantic recall, and MCP policy/audit. |
| Observability | Local structured events and sanitized Langfuse export. |
| Stability | DM sidecar isolation prevents JVM and ONNX from sharing the gateway process. |
