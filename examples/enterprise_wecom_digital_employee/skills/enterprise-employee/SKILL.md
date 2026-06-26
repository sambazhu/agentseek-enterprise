---
name: enterprise-employee
description: Use runtime employee_context correctly for enterprise identity, permissions, organization, and employee-specific answers.
---

# Enterprise Employee Context

Use this skill whenever the user asks for employee-specific information, permissions, workflow eligibility, or actions that depend on who they are.

Rules:

- Prefer `employee_context.name`, `employee_context.oa_account`, `employee_context.primary_org_name`, `employee_context.org_path_label`, `employee_context.dept_name`, `employee_context.belong_to_label`, and `employee_context.role_label` over anything inferred from chat text.
- If `employee_context` is absent, say that identity resolution is not available and ask the user to retry after the runtime configuration is fixed.
- Do not ask the user to re-enter their OA account when the runtime already provides one.
- Treat branch, headquarters, and subsidiary distinctions as permission hints, not as final authorization decisions. Final authorization belongs to the business MCP tool or downstream system.
