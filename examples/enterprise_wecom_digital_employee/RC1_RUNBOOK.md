---
title: How to validate and roll back Enterprise WeCom v0.0.9 RC1
type: how-to
audience: [A4]
runs: yes
verified_on: 2026-07-11
sources:
  - examples/enterprise_wecom_digital_employee/CHANGELOG.md
  - examples/enterprise_wecom_digital_employee/DEPLOYMENT_NOTES.md
  - examples/enterprise_wecom_digital_employee/PRODUCTION_FREEZE.md
  - examples/enterprise_wecom_digital_employee/scripts/prod_check.py
---

# How to validate and roll back Enterprise WeCom v0.0.9 RC1

Use this guide to install the immutable RC1 source, run acceptance checks, and
return to the v0.0.8 GA baseline.

## Prerequisites

- Use the company-network Mac mini or an equivalent configured host.
- Preserve the deployment `.env`, `.agents/mcp.local.json`, DM JDBC driver, and model files outside Git operations.
- Keep `enterprise-wecom-v0.0.8-ga` available as the rollback tag.
- Do not copy runtime files or credentials into the repository.

## Install the release candidate

1. Fetch the immutable tag and check it out.

   ```bash title="not executed in this run"
   git fetch origin --tags
   git checkout enterprise-wecom-v0.0.9-rc1
   ```

   TODO for the operator: run this after the RC1 tag is published.

2. Synchronize the frozen workspace.

   ```bash
   uv sync --frozen --all-packages --all-extras --group plugins
   ```

3. Run the production preflight without printing secret values.

   ```bash title="not executed in this run"
   uv run python examples/enterprise_wecom_digital_employee/scripts/prod_check.py --env-file examples/enterprise_wecom_digital_employee/.env
   ```

   TODO for the operator: run this on the configured Mac mini.

## Run automated acceptance

1. Check the file plugin.

   ```bash
   make check-files
   ```

   Expected result — 47 passed and `ty` clean.

2. Check WeCom and enterprise runtime behavior.

   ```bash
   make test-wecom
   make test-enterprise
   ```

   Expected result — 38 WeCom tests and 79 enterprise tests passed.

3. Render all templates.

   ```bash
   HOME=/tmp PYTHONPATH=. .venv/bin/pytest -q tests/cli_commands/test_templates_render.py
   ```

   Expected result — 25 passed.

4. Verify the frozen MCP implementation.

   ```bash
   git diff 68d7b25 -- contrib/agentseek-enterprise/src/agentseek_enterprise/mcp_policy.py examples/enterprise_wecom_digital_employee/src/enterprise_wecom_digital_employee/tools.py
   ```

   Expected result — no output.

## Run live WeCom acceptance

1. Restart the supervised gateway with the checked-out RC1 source.

   ```bash title="not executed in this run"
   launchctl kickstart -k gui/$(id -u)/com.local.agentseek-enterprise-wecom
   ```

   TODO for the operator: confirm the installed LaunchAgent label before running.

2. Confirm startup health.

   ```bash title="not executed in this run"
   tail -n 200 ~/Library/Logs/agentseek-wecom/gateway.log
   ```

   Expected signals — `Application startup complete`, Uvicorn port `12000`, no
   SIGBUS, no traceback, and no callback response outside HTTP 200.

3. Send the following smoke prompts through WeCom.

   ```text
   我是谁
   帮我记一下明天去深圳出差
   我刚才说要去哪里
   我的长期工作职责是什么
   列一下当前可用的 MCP 工具
   ```

4. Upload and inspect representative files.

   ```text
   数字 PDF：内容是什么？
   扫描 PDF：请读取扫描文字。
   多页签 XLSX：每个班分别多少人？请列出所有班级。
   PPTX：共几页？按顺序列出标题，并概括第 3、4 页。
   TXT：概括文件内容。
   ```

5. Verify file-analysis safety.

   - `analyze_file` reads only the current session's `extracted.md` or `extracted.txt`.
   - Responses contain no host paths, original binary content, tokens, or secrets.
   - Background OCR preserves ordinary text and unrelated sheets.

## Roll back to v0.0.8 GA

1. Stop the RC1 gateway before switching source.

   ```bash title="not executed in this run"
   launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.local.agentseek-enterprise-wecom.plist
   ```

2. Check out the immutable GA baseline and restore its frozen dependencies.

   ```bash title="not executed in this run"
   git checkout enterprise-wecom-v0.0.8-ga
   uv sync --frozen --all-packages --all-extras --group plugins
   ```

3. Start the GA gateway again.

   ```bash title="not executed in this run"
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.agentseek-enterprise-wecom.plist
   ```

4. Repeat identity, memory, MCP, and gateway-health smoke checks.

Rollback preserves deployment secrets and runtime data. It removes v0.0.9 file
intake from the active source while restoring the verified v0.0.8 runtime.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| A file uploaded before restart is unavailable | Current file records are process-local | Upload the file again after restart. |
| File extraction remains pending | MinerU task has not completed | Ask again after completion; CurrentFiles re-polls the stored task. |
| Embedded image remains unparsed | MinerU returned no OCR text | Check `metadata.background_ocr`; `unchanged` is a safe upstream limitation. |
| PPTX takes about 90 seconds | First pass and background OCR both ran | Keep the stream alive and wait for the completed response. |
| Gateway exits with SIGBUS | DM sidecar isolation is not active | Restore `AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=sidecar` or `subprocess`. |

## Related

- `CHANGELOG.md`
- `PRODUCTION_FREEZE.md`
- `DEPLOYMENT_NOTES.md`

