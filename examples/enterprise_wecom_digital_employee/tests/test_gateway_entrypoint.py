from __future__ import annotations

from pathlib import Path


def test_gateway_runner_uses_unbuffered_python_for_redirected_logs() -> None:
    project_root = Path(__file__).parents[1]
    script = (project_root / "scripts" / "run_gateway.sh").read_text(encoding="utf-8")

    assert "python -u examples/enterprise_wecom_digital_employee/scripts/bub_gateway.py" in script
