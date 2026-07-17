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
6. Report every missing or conflicting field. Do not infer a business decision from chat history or source content.
7. Do not start research, call an external model, approve a report, or deliver an artifact during intake.
8. Return only data allowed by the Playbook output contract. The Skill cannot change WorkItem state, budget, tool grants, or policy.
