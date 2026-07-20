---
name: report-writing
description: Build, confirm, approve, and explicitly render a reviewable securities-industry report through discrete, versioned checkpoints. Keep Claims evidence-bound and keep artifact rendering separate from publication and delivery.
---

# Report writing

1. Confirming `ReportOutline vN` is a discrete human checkpoint. That turn must stop after confirmation, tell the employee to send a later `生成可审阅初稿` request, and never automatically prepare Evidence or generate a draft.
2. Require a later employee message that explicitly asks to generate a review draft. Then revalidate the exact confirmed ReportOutline, its ReportBrief, research-plan, gap-decision, and source-set bindings.
3. Call `prepare_report_draft_context` before writing. Use only the returned Evidence excerpts and IDs; do not fill facts from model memory or old conversation text.
4. Preserve every confirmed `section_id`. Submit concise structured Claims to `build_report_draft`; facts and inferences require Evidence IDs from the same section.
5. Express recommendations and risks as such. Keep unsupported or unresolved research questions visible instead of inventing values.
6. Treat the tool-rendered Markdown and quality checks as authoritative. Relay them verbatim and do not rewrite ledger versions, citations, or quality status.
7. If a current draft already exists and the employee requests it again, call `get_current_report_draft` or the idempotent draft tools. Never reproduce Markdown from conversation memory.
8. Confirm a draft only after the employee explicitly names the exact `ReportDraft vN`. Confirmation records requester acceptance of that review draft, not final approval, and the turn must stop after confirmation.
9. Submit a confirmed draft for approval only after an exact `提交 ReportDraft vN 审批` request. Approve it only after the authenticated approver separately says `批准 ReportDraft vN`. Both transitions must use their ledger tools and bind the exact Draft digest.
10. Content approval is not rendering, publication, or delivery. After approval, stop and tell the employee that a separate exact `生成 ReportDraft vN DOCX` request is required.
11. Render only the current approved ReportDraft after that exact request. Use `render_report_docx_artifact`; do not fabricate an artifact ID, filename, digest, or storage result. PDF is not enabled in this slice.
12. A rendered Artifact is still neither published nor delivered. Report only the ledger-backed artifact ID, content digest, current status, and explicit `not_published` / `not_delivered` state. Never expose a host filesystem path.
13. Publish only after a later employee message exactly says `发布 ReportArtifact vN`. Call `publish_report_artifact` and rely on its server-side revalidation of the current Draft, Approval, Artifact bytes, digests, actor, and policy.
14. Publication is an immutable ledger fact, not delivery. Stop after publication and state explicitly that no template card, file, signed link, or download endpoint was produced. Delivery remains unavailable in this slice.
