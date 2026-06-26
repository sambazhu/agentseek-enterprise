from __future__ import annotations

import json
from typing import Any

BELONG_TO_LABELS = {
    "1": "公司总部",
    "2": "金通子公司",
    "3": "金鼎子公司",
    "4": "分支机构",
}

ROLE_LABELS = {
    "1": "总部员工",
    "2": "分支机构负责人",
    "3": "营业部普通员工",
}


def infer_belong_to_and_role(ladp_dn: str | None) -> tuple[str | None, str | None]:
    if not ladp_dn:
        return None, None
    if "公司总部" in ladp_dn:
        return "1", "1"
    if "金通子公司" in ladp_dn:
        return "2", None
    if "金鼎子公司" in ladp_dn:
        return "3", None
    if "分支机构" in ladp_dn:
        return "4", None
    return None, None


def normalize_sex(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "M":
        return "1"
    if text == "F":
        return "0"
    return text or None


def parse_config_map(raw_value: str | None) -> dict[str, str]:
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(value) for key, value in parsed.items() if value is not None}


def calculate_traveler_fields(
    config_map: dict[str, str],
    department: dict[str, Any] | None,
    positions: list[dict[str, Any]],
) -> dict[str, str]:
    traveler_type = "2"
    traveler_identity = "1"
    traveler_investment = "1"
    traveler_delegate = "0"

    dept_no = _clean_string((department or {}).get("fd_no"))
    if dept_no and "travelerTypeSeniorExecutive" in config_map:
        if dept_no == config_map.get("travelerTypeSeniorExecutive"):
            traveler_type = "1"
        if dept_no == config_map.get("travelerIdentityDept"):
            traveler_identity = "0"

    for position in positions:
        fd_no = _clean_string(position.get("fd_no"))
        if not fd_no:
            continue
        if fd_no in config_map.get("travelerTypeChairman", ""):
            traveler_type = "0"
        if fd_no in config_map.get("travelerTypeSupervisor", "") and traveler_type != "0":
            traveler_type = "4"
        if (
            fd_no in config_map.get("travelerTypeGeneralManager", "")
            and traveler_type != "0"
            and traveler_type != "4"
        ):
            traveler_type = "3"
        if fd_no in config_map.get("travelerIdentity", ""):
            traveler_identity = "0"
        if fd_no in config_map.get("travelerInvestment", ""):
            traveler_investment = "0"
        if fd_no in config_map.get("travelerDelegate", ""):
            traveler_delegate = "1"

    return {
        "traveler_type": traveler_type,
        "traveler_identity": traveler_identity,
        "traveler_investment": traveler_investment,
        "traveler_delegate": traveler_delegate,
    }


def _clean_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

