from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_prod_check() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "prod_check.py"
    spec = importlib.util.spec_from_file_location("enterprise_wecom_prod_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check(env: dict[str, str]) -> tuple[list[str], list[str]]:
    module = _load_prod_check()
    report = module.CheckReport()
    module.check_wecom_outbound(env, report)
    return report.failures, report.warnings


def _check_durable(env: dict[str, str], project_root: Path) -> tuple[list[str], list[str]]:
    module = _load_prod_check()
    report = module.CheckReport()
    module.check_wecom_durable(env, project_root, report)
    return report.failures, report.warnings


def test_durable_preflight_accepts_default_memory_mode(tmp_path: Path) -> None:
    failures, warnings = _check_durable({}, tmp_path)

    assert failures == []
    assert warnings == []


def test_durable_preflight_accepts_encrypted_sqlite_mode(tmp_path: Path) -> None:
    failures, warnings = _check_durable(
        {
            "AGENTSEEK_WECOM_DURABLE_MODE": "sqlite",
            "AGENTSEEK_WECOM_DURABLE_SQLITE_PATH": "runtime/wecom.sqlite3",
            "AGENTSEEK_WECOM_DURABLE_SECRET": "dedicated-test-key-material-with-32-characters",
        },
        tmp_path,
    )

    assert failures == []
    assert warnings == []


def test_durable_preflight_rejects_missing_sqlite_secret(tmp_path: Path) -> None:
    failures, _ = _check_durable(
        {
            "AGENTSEEK_WECOM_DURABLE_MODE": "sqlite",
            "AGENTSEEK_WECOM_DURABLE_SQLITE_PATH": "runtime/wecom.sqlite3",
        },
        tmp_path,
    )

    assert failures == [
        "AGENTSEEK_WECOM_DURABLE_SECRET must be a dedicated high-entropy value in sqlite mode"
    ]


def test_outbound_preflight_accepts_disabled_callback_delivery() -> None:
    failures, warnings = _check({
        "AGENTSEEK_WECOM_TRANSPORT_MODE": "callback",
        "AGENTSEEK_WORK_ARTIFACT_DELIVERY_MODE": "disabled",
    })

    assert failures == []
    assert warnings == []


def test_outbound_preflight_rejects_direct_file_on_callback() -> None:
    failures, _ = _check({
        "AGENTSEEK_WECOM_TRANSPORT_MODE": "callback",
        "AGENTSEEK_WORK_ARTIFACT_DELIVERY_MODE": "direct_file",
    })

    assert failures == ["AI Bot callback response_url cannot deliver file messages; use signed_link or disabled"]


def test_outbound_preflight_rejects_unimplemented_transport() -> None:
    failures, _ = _check({"AGENTSEEK_WECOM_TRANSPORT_MODE": "long_connection"})

    assert failures == ["this gateway implements AGENTSEEK_WECOM_TRANSPORT_MODE=callback only"]


def test_outbound_preflight_validates_signed_link_base_url() -> None:
    failures, _ = _check({
        "AGENTSEEK_WECOM_TRANSPORT_MODE": "callback",
        "AGENTSEEK_WORK_ARTIFACT_DELIVERY_MODE": "signed_link",
        "AGENTSEEK_WORK_ARTIFACT_PUBLIC_BASE_URL": "https://reports.example.test/artifacts",
    })

    assert failures == []


def test_outbound_preflight_rejects_unbounded_signed_link_ttl() -> None:
    failures, _warnings = _check({
        "AGENTSEEK_WECOM_TRANSPORT_MODE": "callback",
        "AGENTSEEK_WORK_ARTIFACT_DELIVERY_MODE": "signed_link",
        "AGENTSEEK_WORK_ARTIFACT_PUBLIC_BASE_URL": "https://reports.example.test/artifacts",
        "AGENTSEEK_WORK_ARTIFACT_GRANT_TTL_SECONDS": "3601",
    })

    assert failures == ["AGENTSEEK_WORK_ARTIFACT_GRANT_TTL_SECONDS must be from 1 to 3600"]

    failures, _ = _check({
        "AGENTSEEK_WECOM_TRANSPORT_MODE": "callback",
        "AGENTSEEK_WORK_ARTIFACT_DELIVERY_MODE": "signed_link",
        "AGENTSEEK_WORK_ARTIFACT_PUBLIC_BASE_URL": "http://reports.example.test?token=secret",
    })
    assert failures == ["signed_link Artifact delivery requires a clean HTTPS public base URL"]
