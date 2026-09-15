from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_gateway_runner_uses_unbuffered_python_for_redirected_logs() -> None:
    project_root = Path(__file__).parents[1]
    script = (project_root / "scripts" / "run_gateway.sh").read_text(encoding="utf-8")

    assert "python -u examples/enterprise_wecom_digital_employee/scripts/bub_gateway.py" in script


@pytest.mark.parametrize("inherited", ["", "/unrelated/overlay"])
def test_standard_runner_supplies_same_tree_skill_mcp_without_pth(tmp_path, inherited):
    project = Path(__file__).parents[1]
    repo = project.parents[1]
    target = tmp_path / "checkout" / "examples" / project.name / "scripts"
    target.mkdir(parents=True)
    shutil.copyfile(project / "scripts/run_gateway.sh", target / "run_gateway.sh")
    package = tmp_path / "checkout/contrib/agentseek-skill-mcp/src"
    package.mkdir(parents=True)
    shutil.copytree(repo / "contrib/agentseek-skill-mcp/src/agentseek_skill_mcp", package / "agentseek_skill_mcp")
    binary = tmp_path / "bin"
    binary.mkdir()
    stub = binary / "uv"
    # Replace only uv: execute the actual runner without launching a gateway.
    stub.write_text(f'#!{sys.executable}\nimport os, subprocess, sys\n'
                    'assert sys.argv[1:3] == ["run", "--offline"]\n'
                    'subprocess.run([sys.executable, "-S", "-c", '
                    '"import importlib.util, os; s=importlib.util.find_spec(\'agentseek_skill_mcp\'); '
                    'assert s and s.origin.startswith(os.getcwd()); print(s.origin)"], check=True)\n')
    stub.chmod(0o700)
    log = tmp_path / "gateway.log"
    env = {**os.environ, "PATH": f"{binary}:{os.environ['PATH']}", "PYTHONPATH": inherited,
           "AGENTSEEK_GATEWAY_LOG": str(log), "AGENTSEEK_ENV_FILE": str(tmp_path / "unused.env")}
    subprocess.run(["/bin/bash", str(target / "run_gateway.sh")], env=env, check=True, timeout=10)  # noqa: S603 -- trusted test script
    assert str(package) in log.read_text()
    assert log.stat().st_mode & 0o777 == 0o600
