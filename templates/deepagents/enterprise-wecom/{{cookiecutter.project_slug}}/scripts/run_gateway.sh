#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"

cd "$PROJECT_ROOT"
mkdir -p runtime

export AGENTSEEK_ENV_FILE="${AGENTSEEK_ENV_FILE:-.env}"
export PYTHONPATH="${PROJECT_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

GATEWAY_LOG="${AGENTSEEK_GATEWAY_LOG:-$HOME/Library/Logs/agentseek-wecom/gateway.log}"
mkdir -p "$(dirname "$GATEWAY_LOG")"

exec uv run --offline --env-file "$AGENTSEEK_ENV_FILE" --with jaydebeapi --with JPype1 \
  python scripts/bub_gateway.py gateway \
  --enable-channel wecom \
  --enable-channel mcp.lifecycle \
  --enable-channel skills.lifecycle \
  >> "$GATEWAY_LOG" 2>&1
