---
title: How to qualify Enterprise WeCom v0.0.9 for GA
type: how-to
audience: [A4]
runs: yes
verified_on: 2026-07-12
sources:
  - examples/enterprise_wecom_digital_employee/CHANGELOG.md
  - examples/enterprise_wecom_digital_employee/RC1_RUNBOOK.md
  - examples/enterprise_wecom_digital_employee/DEPLOYMENT_NOTES.md
  - examples/enterprise_wecom_digital_employee/PRODUCTION_FREEZE.md
---

# How to qualify Enterprise WeCom v0.0.9 for GA

Use this guide to decide whether `enterprise-wecom-v0.0.9-rc1` can become the
next immutable production baseline. Do not create or move a GA tag until every
gate passes.

## Prerequisites

- Run the live gates on the company-network Mac mini.
- Keep `enterprise-wecom-v0.0.8-ga` available as the rollback baseline.
- Preserve `.env`, `.agents/mcp.local.json`, runtime data, model files, and JDBC drivers outside Git changes.
- Treat `8128aac4c37a46264477709adf07bd99e5eadb58` as the only valid RC1 tag target.
- Accept the documented Langfuse top-level `release=None` UI limitation, or publish a separately tested RC2.

## Gate 1: Verify immutable refs

1. Fetch both mirrors without changing the running checkout.

   ```bash title="not executed in this run"
   git fetch origin --tags
   git fetch company-gitlab --tags
   ```

2. Verify the RC1 tag on both mirrors.

   ```bash title="not executed in this run"
   git ls-remote --tags origin refs/tags/enterprise-wecom-v0.0.9-rc1
   git ls-remote --tags company-gitlab refs/tags/enterprise-wecom-v0.0.9-rc1
   ```

   Expected target on both mirrors:

   ```text
   8128aac4c37a46264477709adf07bd99e5eadb58
   ```

3. Verify `production` points to the same RC1 commit on both mirrors.

   ```bash title="not executed in this run"
   git ls-remote origin refs/heads/production
   git ls-remote company-gitlab refs/heads/production
   ```

Stop if any ref differs. Do not force-push or recreate the RC1 tag.

## Gate 2: Install from the tag in a clean checkout

1. Clone the tag into a disposable directory.

   ```bash title="not executed in this run"
   git clone --branch enterprise-wecom-v0.0.9-rc1 --depth 1 https://github.com/sambazhu/agentseek-enterprise.git /tmp/agentseek-v0.0.9-ga-audit
   cd /tmp/agentseek-v0.0.9-ga-audit
   ```

2. Synchronize only from the frozen lock.

   ```bash title="not executed in this run"
   uv sync --frozen --all-packages --all-extras --group plugins
   ```

3. Confirm the checkout is exact and clean.

   ```bash title="not executed in this run"
   git rev-parse HEAD
   git status --short
   ```

Expected result — commit `8128aac4c37a46264477709adf07bd99e5eadb58`
and no status output.

Cleanup after the audit:

```bash title="not executed in this run"
rm -rf /tmp/agentseek-v0.0.9-ga-audit
```

## Gate 3: Run automated acceptance

1. Run the file, WeCom, and enterprise suites.

   ```bash
   make check-files
   make test-wecom
   make test-enterprise
   ```

   Expected result — files 47, WeCom 38, and enterprise 79 passed.

2. Render all templates.

   ```bash
   HOME=/tmp PYTHONPATH=. .venv/bin/pytest -q tests/cli_commands/test_templates_render.py
   ```

   Expected result — 25 passed.

3. Verify the frozen MCP implementation.

   ```bash
   git diff 68d7b25 -- contrib/agentseek-enterprise/src/agentseek_enterprise/mcp_policy.py examples/enterprise_wecom_digital_employee/src/enterprise_wecom_digital_employee/tools.py
   ```

   Expected result — no output.

## Gate 4: Run minimum live acceptance

1. Add the release label to the local deployment `.env` without committing it.

   ```text title=".env (local only; not committed)"
   AGENTSEEK_LANGFUSE_RELEASE=enterprise-wecom-v0.0.9-rc1
   ```

2. Restart the supervised gateway from the RC1 checkout.

   ```bash title="not executed in this run"
   launchctl kickstart -k gui/$(id -u)/com.local.agentseek-enterprise-wecom
   ```

3. Confirm startup and callback health.

   ```bash title="not executed in this run"
   tail -n 300 ~/Library/Logs/agentseek-wecom/gateway.log
   ```

   Required signals:

   - `Application startup complete` and Uvicorn port `12000`.
   - Zero SIGBUS, traceback, and callback HTTP response outside 200 after restart.
   - DM sidecar starts or reuses its isolated process.

4. Send the runtime regression prompts.

   ```text
   我是谁
   帮我记一下明天去深圳出差
   我刚才说要去哪里
   请长期记住：我负责 GA 发布验收
   我的长期工作职责是什么
   列一下当前可用的 MCP 工具
   ```

5. Upload one representative file from each critical path.

   ```text
   数字 PDF：概括主要内容。
   扫描 PDF：读取扫描文字。
   多页签 XLSX：列出所有班级并分别统计人数。
   PPTX：按顺序列出所有页面标题并概括第 3、4 页。
   TXT：概括文件内容。
   语音：确认转写内容。
   ```

6. Confirm observability and safety.

   - Local structured events and sanitized Langfuse traces are present.
   - Langfuse metadata carries `enterprise-wecom-v0.0.9-rc1`.
   - Top-level Langfuse runtime trace `release` may remain `None`; this is an accepted UI-only limitation.
   - Outbound replies contain no host paths, original binary content, tokens, or secrets.
   - `runtime/mcp-audit.jsonl` remains separate from model-visible context.

## Gate 5: Record the promotion decision

Record each gate as `PASS` or `BLOCKED` in `DEPLOYMENT_NOTES.md`.

GA eligibility requires:

- Both mirrors point RC1 and `production` to `8128aac`.
- Frozen installation succeeds in the clean checkout.
- All 189 automated checks pass.
- Identity, memory, pgvector, MCP, and representative file paths pass live.
- Gateway health and outbound-data safety pass.
- No code or dependency change occurs after RC1 verification.

If all conditions pass, hand the report back for GA documentation and tag
creation. Recommended GA tag:

```text
enterprise-wecom-v0.0.9-ga
```

## Roll back a failed GA audit

1. Stop the candidate gateway.

   ```bash title="not executed in this run"
   launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.local.agentseek-enterprise-wecom.plist
   ```

2. Restore the v0.0.8 GA source and frozen environment.

   ```bash title="not executed in this run"
   git checkout enterprise-wecom-v0.0.8-ga
   uv sync --frozen --all-packages --all-extras --group plugins
   ```

3. Start the verified GA gateway.

   ```bash title="not executed in this run"
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.agentseek-enterprise-wecom.plist
   ```

Do not delete runtime databases during rollback. v0.0.8 ignores the v0.0.9
file-intake path while preserving the established runtime data stores.

## Related

- `CHANGELOG.md`
- `RC1_RUNBOOK.md`
- `PRODUCTION_FREEZE.md`
- `DEPLOYMENT_NOTES.md`
