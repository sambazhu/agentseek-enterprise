---
name: office-workflow
description: Use MCP tools safely for office workflows such as meeting rooms, travel requests, knowledge lookup, and workflow status.
---

# Office Workflow

Use this skill for comprehensive office tasks such as meeting room booking, travel request submission, internal knowledge lookup, or workflow status checks.

Workflow:

1. Identify the intent and required fields.
2. Use `list_mcp_tools` when you need to discover available business tools.
3. Use `call_mcp_tool` when a suitable MCP tool exists.
4. Ask for missing required fields in one concise message.
5. Before state-changing operations, confirm the action unless the user's current message already gives explicit confirmation.

Never invent booking confirmations, approval numbers, travel policy results, or workflow status. Return the exact result from MCP tools, summarized for WeCom chat.
