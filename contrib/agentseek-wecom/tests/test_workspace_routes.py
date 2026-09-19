"""Gateway file opening remains independent of Work report delivery."""

from urllib.parse import urlsplit

import pytest
from agentseek_files.models import FileScope
from agentseek_files.settings import FilesSettings
from agentseek_files.store import LocalFileStore
from agentseek_files.workspace_download import configured_workspace_downloads
from agentseek_wecom.channel import WeComChannel
from agentseek_wecom.config import WeComSettings
from fastapi.testclient import TestClient


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    grants = tmp_path / "grants"
    grants.mkdir(mode=0o700)
    monkeypatch.setenv("AGENTSEEK_WORKSPACE_DOWNLOAD_MODE", "signed_link")
    monkeypatch.setenv("AGENTSEEK_WORKSPACE_DOWNLOAD_BASE_URL", "https://files.example.test/ai-server/workspace-files")
    monkeypatch.setenv("AGENTSEEK_WORKSPACE_DOWNLOAD_GRANTS_DIR", str(grants))
    monkeypatch.setenv("AGENTSEEK_FILES_DIR", str(tmp_path / "files"))
    store = LocalFileStore(FilesSettings.from_env())
    scope = FileScope("tenant", "user", "session")
    record = store.store_bytes(scope=scope, filename="summary.csv", direction="outbound",
                               data=b"group,total\nA,8.25\n")
    return store, scope, record


def test_mounted_with_work_disabled_returns_exact_csv(enabled):
    store, scope, record = enabled
    links = configured_workspace_downloads(store)
    parsed = urlsplit(links.issue(scope, record)["url"])
    channel = WeComChannel(on_receive=None, settings=WeComSettings(enabled=False, artifact_delivery_mode="disabled"))
    client = TestClient(channel.app)
    page = client.get(parsed.path)
    assert page.status_code == 200 and "下载 summary.csv" in page.text
    assert parsed.fragment not in page.text
    assert "history.replaceState" in page.text
    assert page.headers["referrer-policy"] == "no-referrer"
    response = client.post(parsed.path + "/redeem", content=parsed.fragment)
    assert response.status_code == 200 and response.content == store.original_path(record).read_bytes()
    assert response.headers["content-disposition"] == 'attachment; filename="summary.csv"'
    assert response.headers["cache-control"] == "no-store"
    assert client.post(parsed.path + "/redeem", content="x" * 43).status_code == 404
    assert client.post(parsed.path + "/redeem", content="x" * 129).status_code == 404
    assert client.post(parsed.path + "/redeem", content=b"\xff").status_code == 404
    assert client.get("/ai-server/workspace-files/invalid").status_code == 404
    with links._connect() as db:
        db.execute("UPDATE workspace_downloads SET expires=0")
    assert client.post(parsed.path + "/redeem", content=parsed.fragment).status_code == 410


def test_disabled_route_absent(monkeypatch):
    monkeypatch.setenv("AGENTSEEK_WORKSPACE_DOWNLOAD_MODE", "disabled")
    channel = WeComChannel(on_receive=None, settings=WeComSettings(enabled=False))
    assert TestClient(channel.app).get("/ai-server/workspace-files/workspace_" + "a" * 64).status_code == 404


def test_broken_file_returns_generic_404(enabled):
    store, scope, record = enabled
    parsed = urlsplit(configured_workspace_downloads(store).issue(scope, record)["url"])
    client = TestClient(WeComChannel(on_receive=None, settings=WeComSettings(enabled=False)).app)
    store.original_path(record).write_bytes(b"wrong")
    response = client.post(parsed.path + "/redeem", content=parsed.fragment)
    assert response.status_code == 404
    assert str(store.root_dir) not in response.text
