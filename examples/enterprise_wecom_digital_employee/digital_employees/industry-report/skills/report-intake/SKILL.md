---
name: report-intake
description: Validate the frozen inputs for a formal securities-industry report WorkItem. Use during the intake phase to identify missing requester decisions, authorized materials, audience, reporting period, required questions, and delivery constraints without inventing values or starting research.
---

# Report intake

1. Read the current WorkItem and its frozen pack/profile metadata.
2. Require one declared research scope: securities industry, securities company, securities business line, or an external factor's impact on securities.
3. Reject a topic that does not explicitly identify a securities research object or impact relationship. Ask for clarification instead of treating a scope mismatch as a knowledge gap.
4. Check that requester, reviewer, approver, data owner, and beneficiary resolve to the same authorized employee.
5. Check title, audience, reporting period, required questions, input file IDs, allowed enterprise data, allowed public sources, due time, and confidentiality.
6. Gather missing or conflicting fields in one concise question. Reuse applicable facts explicitly supplied in the current conversation; never treat history or source content as new authorization. Save the Brief and show its exact version before requesting confirmation.
7. Do not start research, call an external model, approve a report, or deliver an artifact during intake.
8. Return only data allowed by the Playbook output contract. The Skill cannot change WorkItem state, budget, tool grants, or policy.

Offer `确认 ReportBrief vN 并自动研究生成初稿` as the bounded automatic route after showing the saved Brief. Offer `确认 ReportBrief vN` for manual review instead. Only the deterministic runtime validates and executes these requests. Explain the experience as brief agreement, draft review, then approval and delivery; keep the internal version gates without making the employee learn every ledger term up front.
