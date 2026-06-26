from __future__ import annotations

import importlib
import re
from typing import Any, Protocol

from agentseek_enterprise.identity.models import EmployeeContext, IdentityDbSettings
from agentseek_enterprise.identity.rules import (
    BELONG_TO_LABELS,
    ROLE_LABELS,
    calculate_traveler_fields,
    infer_belong_to_and_role,
    normalize_sex,
    parse_config_map,
)


class DbCursor(Protocol):
    description: Any

    def execute(self, sql: str, params: Any = None) -> Any: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> Any: ...

    def close(self) -> Any: ...


class DbConnection(Protocol):
    def cursor(self) -> DbCursor: ...

    def close(self) -> Any: ...


class DmStaffIdentityProvider:
    """Read employee identity directly from the OA DM database."""

    def __init__(self, settings: IdentityDbSettings | None = None, connection: DbConnection | None = None):
        self.settings = settings or IdentityDbSettings.from_env()
        self._connection = connection
        self._schema = self._validate_identifier(self.settings.schema_name)

    def get_employee_context(self, oa_account: str) -> EmployeeContext | None:
        oa_account = oa_account.strip()
        if not oa_account:
            return None

        connection = self._connection or self._connect()
        should_close = self._connection is None
        try:
            staff = self._select_staff_by_login_name(connection, oa_account)
            if staff is None:
                return None

            org_path = self._select_org_path(
                connection,
                hierarchy_id=staff.get("hierarchy_id"),
                person_id=str(staff["id"]),
                leaf_org_id=_optional_str(staff.get("parent_id")),
            )
            org_path_label = _format_org_path_label(org_path)
            primary_org = org_path[0] if org_path else None
            positions = self._select_positions(connection, str(staff["id"]))
            department = self._select_department(connection, str(staff["parent_id"])) if staff.get("parent_id") else None
            config = self._select_base_config(connection, "businessTripsAllocation")
            traveler_fields = calculate_traveler_fields(
                parse_config_map(config.get("value") if config else None),
                department,
                positions,
            )
            belong_to, role = infer_belong_to_and_role(staff.get("ladp_dn"))

            return EmployeeContext(
                user_id=str(staff["id"]),
                employee_no=None,
                oa_account=str(staff["fd_login_name"]),
                name=str(staff["fd_name"]),
                sex=normalize_sex(staff.get("fd_sex")),
                dept_id=_optional_str(staff.get("dept_id") or staff.get("parent_id")),
                dept_name=_optional_str(staff.get("dept_name")),
                org_path=org_path,
                org_path_label=org_path_label,
                primary_org_id=_optional_str(primary_org.get("id")) if primary_org else None,
                primary_org_name=_optional_str(primary_org.get("name")) if primary_org else None,
                post=_optional_str(staff.get("post")),
                ladp_dn=_optional_str(staff.get("ladp_dn")),
                hierarchy_id=_optional_str(staff.get("hierarchy_id")),
                belong_to=belong_to,
                belong_to_label=BELONG_TO_LABELS.get(belong_to) if belong_to else None,
                role=role,
                role_label=ROLE_LABELS.get(role) if role else None,
                **traveler_fields,
            )
        finally:
            if should_close:
                connection.close()

    def _connect(self) -> DbConnection:
        try:
            driver = importlib.import_module(self.settings.driver_module)
        except ModuleNotFoundError as exc:
            msg = (
                f"Missing DM database driver module {self.settings.driver_module!r}. "
                "Install the PyPI package 'dmpython' for Linux/Windows, configure the official dmPython driver, "
                "or use AGENTSEEK_IDENTITY_DM_DRIVER_MODULE=agentseek_enterprise.identity.jdbc_driver locally."
            )
            raise RuntimeError(msg) from exc

        return driver.connect(
            user=self.settings.user,
            password=self.settings.password,
            server=self.settings.host,
            port=self.settings.port,
        )

    def _select_staff_by_login_name(self, connection: DbConnection, login_name: str) -> dict[str, Any] | None:
        sql = f"""
            SELECT
                a.FD_ID AS id,
                e.FD_NAME AS fd_name,
                a.FD_LOGIN_NAME AS fd_login_name,
                a.FD_SEX AS fd_sex,
                e.FD_PARENTID AS parent_id,
                e.FD_MEMO AS post,
                (
                    SELECT FD_NAME
                    FROM {self._table("SYS_ORG_ELEMENT")}
                    WHERE FD_ID = e.FD_PARENTID
                ) AS dept_name,
                e.FD_LDAP_DN AS ladp_dn,
                e.FD_PARENTID AS dept_id,
                e.FD_HIERARCHY_ID AS hierarchy_id
            FROM {self._table("SYS_ORG_PERSON")} a
            INNER JOIN {self._table("SYS_ORG_ELEMENT")} e ON a.FD_ID = e.FD_ID
            WHERE e.FD_ORG_TYPE = 8
              AND a.FD_LOGIN_NAME = {self._placeholder(1)}
        """
        return self._fetch_one(connection, sql, (login_name,))

    def _select_department(self, connection: DbConnection, department_id: str) -> dict[str, Any] | None:
        sql = f"""
            SELECT
                FD_ID AS fd_id,
                FD_NO AS fd_no,
                FD_NAME AS fd_name,
                FD_PARENTID AS fd_parent_id,
                FD_ORG_TYPE AS fd_org_type
            FROM {self._table("SYS_ORG_ELEMENT")}
            WHERE FD_ID = {self._placeholder(1)}
              AND FD_ORG_TYPE = {self._placeholder(2)}
        """
        return self._fetch_one(connection, sql, (department_id, 2))

    def _select_org_path(
        self,
        connection: DbConnection,
        *,
        hierarchy_id: Any,
        person_id: str,
        leaf_org_id: str | None,
    ) -> list[dict[str, str | None]]:
        hierarchy_ids = _parse_hierarchy_ids(hierarchy_id)
        if hierarchy_ids:
            org_ids = [org_id for org_id in hierarchy_ids if org_id != person_id]
            path = self._select_org_path_by_ids(connection, org_ids)
            if path:
                return path
        if leaf_org_id is None:
            return []
        return self._select_org_path_by_parent_chain(connection, leaf_org_id)

    def _select_org_path_by_ids(self, connection: DbConnection, org_ids: list[str]) -> list[dict[str, str | None]]:
        if not org_ids:
            return []

        placeholders = ", ".join(self._placeholder(index) for index in range(1, len(org_ids) + 1))
        sql = f"""
            SELECT
                FD_ID AS fd_id,
                FD_NO AS fd_no,
                FD_NAME AS fd_name,
                FD_PARENTID AS fd_parent_id,
                FD_ORG_TYPE AS fd_org_type
            FROM {self._table("SYS_ORG_ELEMENT")}
            WHERE FD_ID IN ({placeholders})
              AND FD_ORG_TYPE = 2
        """
        rows = self._fetch_all(connection, sql, tuple(org_ids))
        rows_by_id = {str(row.get("fd_id")): _org_path_node(row) for row in rows if row.get("fd_id") is not None}
        ordered = [rows_by_id[org_id] for org_id in org_ids if org_id in rows_by_id]
        return _trim_org_root(ordered)

    def _select_org_path_by_parent_chain(self, connection: DbConnection, leaf_org_id: str) -> list[dict[str, str | None]]:
        path: list[dict[str, str | None]] = []
        current_id: str | None = leaf_org_id
        visited: set[str] = set()
        while current_id and current_id not in visited and len(path) < 32:
            visited.add(current_id)
            row = self._select_department(connection, current_id)
            if row is None:
                break
            path.append(_org_path_node(row))
            current_id = _optional_str(row.get("fd_parent_id"))
        path.reverse()
        return _trim_org_root(path)

    def _select_positions(self, connection: DbConnection, person_id: str) -> list[dict[str, Any]]:
        sql = f"""
            SELECT
                e.FD_ID AS fd_id,
                e.FD_NO AS fd_no,
                e.FD_NAME AS fd_name,
                e.FD_PARENTID AS fd_parent_id,
                e.FD_ORG_TYPE AS fd_org_type
            FROM {self._table("SYS_ORG_POST_PERSON")} a
            LEFT JOIN {self._table("SYS_ORG_ELEMENT")} e ON a.FD_POSTID = e.FD_ID
            WHERE a.FD_PERSONID = {self._placeholder(1)}
        """
        return self._fetch_all(connection, sql, (person_id,))

    def _select_base_config(self, connection: DbConnection, key: str) -> dict[str, Any] | None:
        sql = f"""
            SELECT
                FD_ID AS id,
                FD_KEY AS key,
                FD_VALUE AS value,
                FD_MARK AS remark
            FROM {self._table("WKZQ_BASIC_CONFIGURE")}
            WHERE FD_KEY = {self._placeholder(1)}
        """
        return self._fetch_one(connection, sql, (key,))

    def _fetch_one(self, connection: DbConnection, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params)
            row = cursor.fetchone()
            if row is None:
                return None
            return _row_to_dict(cursor.description, row)
        finally:
            cursor.close()

    def _fetch_all(self, connection: DbConnection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params)
            return [_row_to_dict(cursor.description, row) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def _placeholder(self, index: int) -> str:
        paramstyle = self.settings.paramstyle.lower()
        if paramstyle == "qmark":
            return "?"
        if paramstyle == "format":
            return "%s"
        if paramstyle == "numeric":
            return f":{index}"
        msg = f"Unsupported AGENTSEEK_IDENTITY_DM_PARAMSTYLE={self.settings.paramstyle!r}"
        raise ValueError(msg)

    def _table(self, table_name: str) -> str:
        return f"{self._schema}.{self._validate_identifier(table_name)}"

    @staticmethod
    def _validate_identifier(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            msg = f"Unsafe database identifier: {value!r}"
            raise ValueError(msg)
        return value.upper()


def _row_to_dict(description: Any, row: Any) -> dict[str, Any]:
    columns = [str(column[0]).lower() for column in description]
    return dict(zip(columns, row, strict=False))


def _parse_hierarchy_ids(value: Any) -> list[str]:
    text = _optional_str(value)
    if text is None:
        return []
    return [part for part in text.split("x") if part]


def _org_path_node(row: dict[str, Any]) -> dict[str, str | None]:
    return {
        "id": _optional_str(row.get("fd_id")),
        "no": _optional_str(row.get("fd_no")),
        "name": _optional_str(row.get("fd_name")),
        "parent_id": _optional_str(row.get("fd_parent_id")),
        "org_type": _optional_str(row.get("fd_org_type")),
    }


def _trim_org_root(path: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    trimmed = list(path)
    while trimmed and (trimmed[0].get("parent_id") is None or trimmed[0].get("name") == "五矿证券"):
        trimmed.pop(0)
    return trimmed


def _format_org_path_label(path: list[dict[str, str | None]]) -> str | None:
    label = " / ".join(str(node["name"]) for node in path if node.get("name"))
    return label or None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
