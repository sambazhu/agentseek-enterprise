"""Process settings must survive project dotenv loading and alias mapping."""
import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize("process,expected", [
    ({"BUB_LANGCHAIN_SPEC": "process-bub"}, "process-bub"),
    ({"AGENTSEEK_LANGCHAIN_SPEC": "process-agent"}, "process-agent"),
    ({"BUB_LANGCHAIN_SPEC": "process-bub", "AGENTSEEK_LANGCHAIN_SPEC": "process-agent"}, "process-bub"),
    ({}, "file-bub"),
])
def test_env_priority(tmp_path, monkeypatch, process, expected):
    spec = importlib.util.spec_from_file_location("gateway_boot", Path(__file__).parents[1] / "scripts/bub_gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dotenv = tmp_path / "settings.env"
    dotenv.write_text("AGENTSEEK_LANGCHAIN_SPEC=file-agent\nBUB_LANGCHAIN_SPEC=file-bub\n")
    for key in ("AGENTSEEK_LANGCHAIN_SPEC", "BUB_LANGCHAIN_SPEC"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENTSEEK_ENV_FILE", str(dotenv))
    for key, value in process.items():
        monkeypatch.setenv(key, value)
    module._load_project_env_file()
    module._apply_project_bub_aliases()
    assert module.os.environ["BUB_LANGCHAIN_SPEC"] == expected
