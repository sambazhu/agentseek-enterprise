from __future__ import annotations

import json
from typing import Any

from agentseek.enterprise.identity import DmStaffIdentityProvider, IdentityDbSettings


class FakeConnection:
    def __init__(self, result_sets: list[tuple[list[str], list[tuple[Any, ...]]]]) -> None:
        self.result_sets = result_sets
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False

    def cursor(self) -> "FakeCursor":
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.description: list[tuple[str]] = []
        self.rows: list[tuple[Any, ...]] = []
        self.closed = False

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
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
                        "hierarchy-1",
                    )
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
    assert context.post == "软件开发岗"
    assert context.belong_to == "1"
    assert context.belong_to_label == "公司总部"
    assert context.role == "1"
    assert context.role_label == "总部员工"
    assert context.traveler_type == "0"
    assert context.traveler_investment == "0"
    assert context.to_java_api_record()["fdLoginName"] == "chenkang2"
    assert connection.executed[0][1] == ("chenkang2",)


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
