---
name: report-writing
description: Build a reviewable securities-industry Markdown draft from a confirmed ReportOutline and registered EvidenceRecords. Use only after the outline confirmation gate; keep factual and inferential Claims evidence-bound, disclose unresolved questions, and never imply final approval or artifact delivery.
---

# Report writing

1. Require the current exact `ReportOutline` to be confirmed and revalidate its ReportBrief, research-plan, gap-decision, and source-set bindings.
2. Call `prepare_report_draft_context` before writing. Use only the returned Evidence excerpts and IDs; do not fill facts from model memory or old conversation text.
3. Preserve every confirmed `section_id`. Submit concise structured Claims to `build_report_draft`; facts and inferences require Evidence IDs from the same section.
4. Express recommendations and risks as such. Keep unsupported or unresolved research questions visible instead of inventing values.
5. Treat the tool-rendered Markdown and quality checks as authoritative. Relay them verbatim and do not rewrite ledger versions, citations, or quality status.
6. The M3-02 output is a provisional review draft. Do not approve it, publish it, generate DOCX/PDF, or deliver an artifact.
