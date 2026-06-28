from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any

import pytest
from agentseek_enterprise.identity import (
    DmStaffIdentityProvider,
    EmployeeContext,
    IdentityDbSettings,
    dm_staff_provider,
    dm_staff_sidecar,
)


class FakeConnection:
    def __init__(self, result_sets: list[tuple[list[str], list[tuple[Any, ...]]]]) -> None:
        self.result_sets = result_sets
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.description: list[tuple[str]] = []
        self.rows: list[tuple[Any, ...]] = []
        self.closed = False

    def execute(self, sql: str, params: Any = None) -> None:
        self.connection.executed.append((sql, params))
        columns, rows = self.connection.result_sets.pop(0)
        self.description = [(column,) for column in columns]
        self.rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def close(self) -> None:
        self.closed = True


def test_dm_staff_identity_provider_normalizes_employee_context() -> None:
    config = {
        "travelerTypeChairman": "POST-CHAIRMAN",
        "travelerIdentity": "POST-SMD",
        "travelerInvestment": "POST-IB",
        "travelerDelegate": "POST-DELEGATE",
    }
    connection = FakeConnection(
        [
            (
                [
                    "id",
                    "fd_name",
                    "fd_login_name",
                    "fd_sex",
                    "parent_id",
                    "post",
                    "dept_name",
                    "ladp_dn",
                    "dept_id",
                    "hierarchy_id",
                ],
                [
                    (
                        "person-1",
                        "陈康",
                        "chenkang2",
                        "M",
                        "dept-1",
                        "软件开发岗",
                        "财富管理研发团队",
                        "CN=陈康,OU=财富管理研发团队,OU=信息技术部,OU=公司总部,OU=五矿证券",
                        "dept-1",
                        "xrootxcompanyxinfo-techxdept-1xperson-1x",
                    )
                ],
            ),
            (
                ["fd_id", "fd_no", "fd_name", "fd_parent_id", "fd_org_type"],
                [
                    ("dept-1", "DEPT-RD", "财富管理研发团队", "info-tech", "2"),
                    ("root", "ROOT", "五矿证券", None, "2"),
                    ("info-tech", "DEPT-IT", "信息技术部", "company", "2"),
                    ("company", "HQ", "公司总部", "root", "2"),
                ],
            ),
            (
                ["fd_id", "fd_no", "fd_name", "fd_parent_id", "fd_org_type"],
                [
                    ("post-1", "POST-CHAIRMAN", "董事长", "dept-1", "4"),
                    ("post-2", "POST-IB", "投行二级部门负责人", "dept-1", "4"),
                ],
            ),
            (
                ["fd_id", "fd_no", "fd_name", "fd_parent_id", "fd_org_type"],
                [("dept-1", "DEPT-RD", "财富管理研发团队", "parent-dept", "2")],
            ),
            (
                ["id", "key", "value", "remark"],
                [("cfg-1", "businessTripsAllocation", json.dumps(config, ensure_ascii=False), "差旅规则")],
            ),
        ]
    )
    settings = IdentityDbSettings(password="secret", schema_name="DBO")
    provider = DmStaffIdentityProvider(settings=settings, connection=connection)

    context = provider.get_employee_context("chenkang2")

    assert context is not None
    assert context.user_id == "person-1"
    assert context.oa_account == "chenkang2"
    assert context.name == "陈康"
    assert context.sex == "1"
    assert context.dept_id == "dept-1"
    assert context.dept_name == "财富管理研发团队"
    assert context.primary_org_name == "公司总部"
    assert context.org_path_label == "公司总部 / 信息技术部 / 财富管理研发团队"
    assert [node["name"] for node in context.org_path] == ["公司总部", "信息技术部", "财富管理研发团队"]
    assert context.post == "软件开发岗"
    assert context.belong_to == "1"
    assert context.belong_to_label == "公司总部"
    assert context.role == "1"
    assert context.role_label == "总部员工"
    assert context.traveler_type == "0"
    assert context.traveler_investment == "0"
    assert context.to_java_api_record()["fdLoginName"] == "chenkang2"
    assert context.to_java_api_record()["orgPathLabel"] == "公司总部 / 信息技术部 / 财富管理研发团队"
    assert connection.executed[0][1] == ("chenkang2",)
    assert connection.executed[1][1] == ("root", "company", "info-tech", "dept-1")


def test_dm_staff_identity_provider_returns_none_when_employee_missing() -> None:
    connection = FakeConnection(
        [
            (
                [
                    "id",
                    "fd_name",
                    "fd_login_name",
                    "fd_sex",
                    "parent_id",
                    "post",
                    "dept_name",
                    "ladp_dn",
                    "dept_id",
                    "hierarchy_id",
                ],
                [],
            ),
        ]
    )
    settings = IdentityDbSettings(password="secret", schema_name="DBO")
    provider = DmStaffIdentityProvider(settings=settings, connection=connection)

    assert provider.get_employee_context("missing") is None


def test_identity_db_settings_loads_project_env_file(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTSEEK_IDENTITY_DM_PASSWORD", raising=False)
    monkeypatch.delenv("AGENTSEEK_IDENTITY_DM_HOST", raising=False)
    monkeypatch.delenv("AGENTSEEK_ENV_FILE", raising=False)
    (tmp_path / ".env").write_text(
        "AGENTSEEK_ENV_FILE=project.env\n",
        encoding="utf-8",
    )
    expected_password = "sec" + "ret"
    (tmp_path / "project.env").write_text(
        f"AGENTSEEK_IDENTITY_DM_PASSWORD={expected_password}\n"
        "AGENTSEEK_IDENTITY_DM_HOST=dm.example.internal\n",
        encoding="utf-8",
    )

    settings = IdentityDbSettings.from_env()

    assert settings.password == expected_password
    assert settings.host == "dm.example.internal"


def test_dm_staff_identity_provider_can_query_via_subprocess(monkeypatch: Any) -> None:
    context = EmployeeContext(user_id="person-1", oa_account="chenkang2", name="陈康")
    calls: list[dict[str, Any]] = []

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ok": True, "employee_context": context.to_dict()}, ensure_ascii=False),
            stderr="",
        )

    monkeypatch.setenv("AGENTSEEK_IDENTITY_DM_EXECUTION_MODE", "subprocess")
    monkeypatch.setattr(dm_staff_provider.subprocess, "run", fake_run)
    provider = DmStaffIdentityProvider(settings=IdentityDbSettings(password="secret"))

    result = provider.get_employee_context("chenkang2")

    assert result is not None
    assert result.oa_account == "chenkang2"
    assert result.name == "陈康"
    assert calls[0]["command"][-2:] == ["--oa", "chenkang2"]
    assert calls[0]["env"]["AGENTSEEK_IDENTITY_DM_EXECUTION_MODE"] == "in_process"


def test_dm_staff_identity_provider_subprocess_not_found(monkeypatch: Any) -> None:
    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        del command, kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ok": True, "employee_context": None}),
            stderr="",
        )

    monkeypatch.setenv("AGENTSEEK_IDENTITY_DM_EXECUTION_MODE", "subprocess")
    monkeypatch.setattr(dm_staff_provider.subprocess, "run", fake_run)
    provider = DmStaffIdentityProvider(settings=IdentityDbSettings(password="secret"))

    assert provider.get_employee_context("missing") is None


def test_dm_staff_identity_provider_subprocess_error(monkeypatch: Any) -> None:
    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        del command, kwargs
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"ok": False, "error_type": "DMException", "error": "网络通信异常"}),
            stderr="",
        )

    monkeypatch.setenv("AGENTSEEK_IDENTITY_DM_EXECUTION_MODE", "subprocess")
    monkeypatch.setattr(dm_staff_provider.subprocess, "run", fake_run)
    provider = DmStaffIdentityProvider(settings=IdentityDbSettings(password="secret"))

    with pytest.raises(RuntimeError, match="DMException"):
        provider.get_employee_context("chenkang2")


def test_dm_staff_identity_provider_can_query_via_persistent_sidecar(monkeypatch: Any) -> None:
    context = EmployeeContext(user_id="person-1", oa_account="chenkang2", name="陈康")
    instances: list[Any] = []

    class FakeSidecarClient:
        def __init__(self) -> None:
            self.lookups: list[str] = []
            self.closed = False
            instances.append(self)

        def lookup(self, oa_account: str) -> EmployeeContext | None:
            self.lookups.append(oa_account)
            return context

        def close(self) -> None:
            self.closed = True

    monkeypatch.setenv("AGENTSEEK_IDENTITY_DM_EXECUTION_MODE", "sidecar")
    monkeypatch.setattr(dm_staff_provider, "_DmIdentitySidecarClient", FakeSidecarClient)
    provider = DmStaffIdentityProvider(settings=IdentityDbSettings(password="secret"))

    first = provider.get_employee_context("chenkang2")
    second = provider.get_employee_context("chenkang2")
    provider.close()

    assert first is not None
    assert second is not None
    assert first.oa_account == "chenkang2"
    assert len(instances) == 1
    assert instances[0].lookups == ["chenkang2", "chenkang2"]
    assert instances[0].closed is True


def test_dm_staff_sidecar_outputs_employee_context(monkeypatch: Any, capsys: Any) -> None:
    context = EmployeeContext(user_id="person-1", oa_account="chenkang2", name="陈康")

    class FakeProvider:
        def get_employee_context(self, oa_account: str) -> EmployeeContext | None:
            assert oa_account == "chenkang2"
            return context

    monkeypatch.setenv("AGENTSEEK_IDENTITY_DM_EXECUTION_MODE", "subprocess")
    monkeypatch.setattr(dm_staff_sidecar, "DmStaffIdentityProvider", FakeProvider)

    assert dm_staff_sidecar.main(["--oa", "chenkang2"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert payload["employee_context"]["oa_account"] == "chenkang2"
    assert payload["employee_context"]["name"] == "陈康"


def test_dm_staff_sidecar_server_handles_multiple_requests(monkeypatch: Any) -> None:
    context = EmployeeContext(user_id="person-1", oa_account="chenkang2", name="陈康")
    instances: list[Any] = []

    class FakeProvider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            self.queries: list[str] = []
            self.closed = False
            instances.append(self)

        def get_employee_context(self, oa_account: str) -> EmployeeContext | None:
            self.queries.append(oa_account)
            if oa_account == "missing":
                return None
            return context

        def reset_connection(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    monkeypatch.setenv("AGENTSEEK_IDENTITY_DM_EXECUTION_MODE", "subprocess")
    monkeypatch.setattr(dm_staff_sidecar, "DmStaffIdentityProvider", FakeProvider)
    monkeypatch.setattr(
        dm_staff_sidecar.sys,
        "stdin",
        io.StringIO('{"oa":"chenkang2"}\n{"oa":"missing"}\n'),
    )
    stdout = io.StringIO()
    monkeypatch.setattr(dm_staff_sidecar.sys, "stdout", stdout)

    assert dm_staff_sidecar.main(["--server"]) == 0
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]

    assert responses[0]["ok"] is True
    assert responses[0]["employee_context"]["oa_account"] == "chenkang2"
    assert responses[1]["ok"] is True
    assert responses[1]["employee_context"] is None
    assert instances[0].queries == ["chenkang2", "missing"]
    assert instances[0].closed is True
