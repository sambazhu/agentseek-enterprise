#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../../.." >/dev/null 2>&1 && pwd)"

cd "$REPO_ROOT"
mkdir -p runtime examples/enterprise_wecom_digital_employee/runtime

export AGENTSEEK_ENV_FILE="${AGENTSEEK_ENV_FILE:-examples/enterprise_wecom_digital_employee/.env}"
export PYTHONPATH="${REPO_ROOT}/examples/enterprise_wecom_digital_employee/src${PYTHONPATH:+:${PYTHONPATH}}"

GATEWAY_LOG="${AGENTSEEK_GATEWAY_LOG:-$HOME/Library/Logs/agentseek-wecom/gateway.log}"
mkdir -p "$(dirname "$GATEWAY_LOG")"

exec uv run --offline --env-file "$AGENTSEEK_ENV_FILE" --with jaydebeapi --with JPype1 \
  python -u examples/enterprise_wecom_digital_employee/scripts/bub_gateway.py gateway \
  --enable-channel wecom \
  --enable-channel mcp.lifecycle \
  --enable-channel skills.lifecycle \
  >> "$GATEWAY_LOG" 2>&1
