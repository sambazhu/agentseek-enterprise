"""Workspace links serve bound persisted bytes; no guest or network required."""

from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest
from agentseek_files.models import FileScope
from agentseek_files.settings import FilesSettings
from agentseek_files.store import LocalFileStore
from agentseek_files.workspace_download import (
    WorkspaceDownloadExpired,
    WorkspaceDownloadNotFound,
    WorkspaceDownloads,
    WorkspaceDownloadSettings,
    configured_workspace_downloads,
)


@pytest.fixture
def workspace(tmp_path):
    store = LocalFileStore(FilesSettings(root_dir=tmp_path / "files"))
    scope = FileScope("tenant", "user", "session")
    record = store.store_bytes(scope=scope, filename="summary.csv", direction="outbound",
                               data=b"group,total\nA,8.25\n")
    grants = tmp_path / "grants"
    grants.mkdir(mode=0o700)
    settings = WorkspaceDownloadSettings("https://files.example.test/ai-server/workspace-files", grants)
    now = [datetime.now(UTC).timestamp()]
    links = WorkspaceDownloads(store=store, settings=settings, clock=lambda: now[0])
    return store, scope, record, settings, now, links


def issued(links, scope, record):
    link = links.issue(scope, record)
    parsed = urlsplit(link["url"])
    return parsed.path.rsplit("/", 1)[1], parsed.fragment, link


def test_link_survives_restart_and_repeated_open_until_expiry(workspace):
    store, scope, record, settings, now, links = workspace
    link_id, token, link = issued(links, scope, record)
    assert token and token not in urlsplit(link["url"]).path
    assert not urlsplit(link["url"]).query
    assert token.encode() not in links.path.read_bytes()
    reopened = WorkspaceDownloads(store=store, settings=settings, clock=lambda: now[0])
    assert reopened.redeem(link_id, token) == store.original_path(record).read_bytes()
    assert reopened.redeem(link_id, token) == b"group,total\nA,8.25\n"
    now[0] = link["expires_epoch"]
    with pytest.raises(WorkspaceDownloadExpired):
        reopened.redeem(link_id, token)


def test_scope_direction_and_token_binding(workspace):
    store, scope, record, _settings, _now, links = workspace
    with pytest.raises(WorkspaceDownloadNotFound):
        links.issue(FileScope("tenant", "other", "session"), record)
    inbound = store.store_bytes(scope=scope, filename="summary.csv", data=b"group,total\nA,8.25\n")
    with pytest.raises(WorkspaceDownloadNotFound):
        links.issue(scope, inbound)
    first_id, token, _ = issued(links, scope, record)
    second_id, second_token, _ = issued(links, scope, record)
    with pytest.raises(WorkspaceDownloadNotFound):
        links.redeem(second_id, token)
    with pytest.raises(WorkspaceDownloadNotFound):
        links.redeem(first_id, second_token)


def test_file_tamper_is_not_served(workspace):
    store, scope, record, _settings, _now, links = workspace
    link_id, token, _ = issued(links, scope, record)
    store.original_path(record).write_bytes(b"group,total\nA,9999\n")
    with pytest.raises(WorkspaceDownloadNotFound):
        links.redeem(link_id, token)


def test_link_does_not_outlive_file(workspace):
    store, scope, record, _settings, now, links = workspace
    record.expires_at = datetime.fromtimestamp(now[0] + 10, UTC).isoformat()
    store.save_record(record)
    _, _, link = issued(links, scope, record)
    assert link["expires_epoch"] == now[0] + 10


def test_same_bytes_refresh_does_not_revoke_existing_link(workspace):
    store, scope, record, _settings, _now, links = workspace
    link_id, token, _ = issued(links, scope, record)
    refreshed = store.store_bytes(scope=scope, filename="summary.csv", direction="outbound",
        data=b"group,total\nA,8.25\n")
    assert refreshed.file_id == record.file_id
    assert links.redeem(link_id, token) == b"group,total\nA,8.25\n"


def test_symlink_file_cannot_be_downloaded(workspace, tmp_path):
    store, scope, record, _settings, _now, links = workspace
    link_id, token, _ = issued(links, scope, record)
    target = tmp_path / "outside.csv"
    target.write_bytes(store.original_path(record).read_bytes())
    store.original_path(record).unlink()
    store.original_path(record).symlink_to(target)
    with pytest.raises(OSError):
        links.redeem(link_id, token)


def test_default_disabled_creates_no_store(monkeypatch):
    monkeypatch.delenv("AGENTSEEK_WORKSPACE_DOWNLOAD_MODE", raising=False)
    assert configured_workspace_downloads() is None


@pytest.mark.parametrize("url", ["http://files.example.test/ai-server/workspace-files",
    "https://user:secret@files.example.test/ai-server/workspace-files",
    "https://files.example.test/ai-server/workspace-files?token=x",
    "https://files.example.test/artifacts"])
def test_invalid_endpoint_rejected(url, tmp_path):
    with pytest.raises(ValueError):
        WorkspaceDownloadSettings(url, tmp_path)
