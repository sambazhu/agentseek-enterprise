#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../../.." >/dev/null 2>&1 && pwd)"

cd "$REPO_ROOT"
mkdir -p runtime examples/enterprise_wecom_digital_employee/runtime

export AGENTSEEK_ENV_FILE="${AGENTSEEK_ENV_FILE:-examples/enterprise_wecom_digital_employee/.env}"
export PYTHONPATH="${REPO_ROOT}/examples/enterprise_wecom_digital_employee/src${PYTHONPATH:+:${PYTHONPATH}}"

exec uv run --offline --with jaydebeapi --with JPype1 agentseek gateway \
  --enable-channel wecom \
  --enable-channel mcp.lifecycle \
  --enable-channel skills.lifecycle
