---
name: report-writing
description: Build a reviewable securities-industry Markdown draft from a confirmed ReportOutline and registered EvidenceRecords. Use only after the outline confirmation gate; keep factual and inferential Claims evidence-bound, disclose unresolved questions, and never imply final approval or artifact delivery.
---

# Report writing

1. Confirming `ReportOutline vN` is a discrete human checkpoint. That turn must stop after confirmation; do not automatically prepare Evidence or generate a draft.
2. Require a later employee message that explicitly asks to generate a review draft. Then revalidate the exact confirmed ReportOutline, its ReportBrief, research-plan, gap-decision, and source-set bindings.
3. Call `prepare_report_draft_context` before writing. Use only the returned Evidence excerpts and IDs; do not fill facts from model memory or old conversation text.
4. Preserve every confirmed `section_id`. Submit concise structured Claims to `build_report_draft`; facts and inferences require Evidence IDs from the same section.
5. Express recommendations and risks as such. Keep unsupported or unresolved research questions visible instead of inventing values.
6. Treat the tool-rendered Markdown and quality checks as authoritative. Relay them verbatim and do not rewrite ledger versions, citations, or quality status.
7. Confirm a draft only after the employee explicitly names the exact `ReportDraft vN`. Confirmation records acceptance of that review draft, not final approval.
8. Do not approve or publish the report, generate DOCX/PDF, or deliver an artifact.
