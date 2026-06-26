from __future__ import annotations

import os
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EmployeeContext:
    """Normalized employee identity used by the agent runtime."""

    user_id: str
    oa_account: str
    name: str
    employee_no: str | None = None
    sex: str | None = None
    dept_id: str | None = None
    dept_name: str | None = None
    org_path: list[dict[str, str | None]] = field(default_factory=list)
    org_path_label: str | None = None
    primary_org_id: str | None = None
    primary_org_name: str | None = None
    post: str | None = None
    ladp_dn: str | None = None
    hierarchy_id: str | None = None
    belong_to: str | None = None
    belong_to_label: str | None = None
    role: str | None = None
    role_label: str | None = None
    traveler_type: str | None = None
    traveler_identity: str | None = None
    traveler_investment: str | None = None
    traveler_delegate: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_java_api_record(self) -> dict[str, Any]:
        """Return the field shape used by the old Java endpoint."""
        return {
            "id": self.user_id,
            "ygbh": self.employee_no,
            "fdName": self.name,
            "fdLoginName": self.oa_account,
            "fdSex": self.sex,
            "department": None,
            "position": None,
            "post": self.post,
            "deptName": self.dept_name,
            "parentId": self.dept_id,
            "deptId": self.dept_id,
            "orgPath": self.org_path,
            "orgPathLabel": self.org_path_label,
            "primaryOrgId": self.primary_org_id,
            "primaryOrgName": self.primary_org_name,
            "ladpDn": self.ladp_dn,
            "hierarchyId": self.hierarchy_id,
            "role": self.role,
            "belongTo": self.belong_to,
            "travelerType": self.traveler_type,
            "travelerIdentity": self.traveler_identity,
            "travelerInvestment": self.traveler_investment,
            "travelerDelegate": self.traveler_delegate,
        }


@dataclass(frozen=True)
class IdentityDbSettings:
    """Settings for the runtime employee identity database."""

    password: str
    host: str = "127.0.0.1"
    port: int = 5236
    user: str = "dbo"
    schema_name: str = "DBO"
    driver_module: str = "dmPython"
    paramstyle: str = "qmark"

    @classmethod
    def from_env(cls) -> IdentityDbSettings:
        _load_dotenv_if_present(Path.cwd() / ".env")
        password = _required_env("AGENTSEEK_IDENTITY_DM_PASSWORD")
        return cls(
            host=os.environ.get("AGENTSEEK_IDENTITY_DM_HOST", "127.0.0.1"),
            port=int(os.environ.get("AGENTSEEK_IDENTITY_DM_PORT", "5236")),
            user=os.environ.get("AGENTSEEK_IDENTITY_DM_USER", "dbo"),
            password=password,
            schema_name=os.environ.get("AGENTSEEK_IDENTITY_DM_SCHEMA", "DBO"),
            driver_module=os.environ.get("AGENTSEEK_IDENTITY_DM_DRIVER_MODULE", "dmPython"),
            paramstyle=os.environ.get("AGENTSEEK_IDENTITY_DM_PARAMSTYLE", "qmark"),
        )


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        msg = f"Missing required environment variable: {name}"
        raise RuntimeError(msg)
    return value


def _load_dotenv_if_present(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
            os.environ[key] = parsed[0] if parsed else ""
        except ValueError:
            os.environ[key] = value.strip().strip("'\"")
