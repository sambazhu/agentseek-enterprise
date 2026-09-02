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


def _check_transport(env: dict[str, str]) -> tuple[list[str], list[str]]:
    module = _load_prod_check()
    report = module.CheckReport()
    module.check_wecom(env, report)
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


def test_outbound_preflight_accepts_long_connection_with_durable_store() -> None:
    failures, _ = _check({
        "AGENTSEEK_WECOM_TRANSPORT_MODE": "long_connection",
        "AGENTSEEK_WECOM_DURABLE_MODE": "sqlite",
    })

    assert failures == []


def test_outbound_preflight_requires_durable_store_for_long_connection() -> None:
    failures, _ = _check({"AGENTSEEK_WECOM_TRANSPORT_MODE": "long_connection"})

    assert failures == [
        "long_connection production deployment requires AGENTSEEK_WECOM_DURABLE_MODE=sqlite"
    ]


def test_transport_preflight_accepts_long_connection_credentials() -> None:
    failures, _ = _check_transport({
        "AGENTSEEK_WECOM_TRANSPORT_MODE": "long_connection",
        "AGENTSEEK_WECOM_LONG_CONNECTION_BOT_ID": "bot-1",
        "AGENTSEEK_WECOM_LONG_CONNECTION_SECRET": "dedicated-secret",
        "AGENTSEEK_WECOM_LONG_CONNECTION_LOCK_PATH": "runtime/wecom-long.lock",
        "AGENTSEEK_WECOM_CORP_ID": "corp-1",
        "AGENTSEEK_WECOM_APP_SECRET": "app-secret",
        "AGENTSEEK_WECOM_USERID_RESOLVE_MODE": "openuserid_to_userid",
    })

    assert failures == []


def test_transport_preflight_accepts_supplementary_application() -> None:
    failures, warnings = _check_transport({
        "AGENTSEEK_WECOM_TRANSPORT_MODE": "callback",
        "AGENTSEEK_WECOM_CALLBACK_PATH": "/ai-bot/callback/demo/bot-1",
        "AGENTSEEK_WECOM_TOKEN": "bot-token",
        "AGENTSEEK_WECOM_ENCODING_AES_KEY": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
        "AGENTSEEK_WECOM_CORP_ID": "corp-1",
        "AGENTSEEK_WECOM_APP_SECRET": "identity-helper-secret",
        "AGENTSEEK_WECOM_USERID_RESOLVE_MODE": "openuserid_to_userid",
        "AGENTSEEK_WECOM_DURABLE_MODE": "sqlite",
        "AGENTSEEK_WECOM_APP_TRANSPORT_ENABLED": "true",
        "AGENTSEEK_WECOM_APP_AGENT_ID": "1000005",
        "AGENTSEEK_WECOM_APP_TRANSPORT_SECRET": "application-secret",
        "AGENTSEEK_WECOM_APP_CALLBACK_PATH": "/wecom/app/callback",
        "AGENTSEEK_WECOM_APP_CALLBACK_TOKEN": "AppCallbackToken1",
        "AGENTSEEK_WECOM_APP_CALLBACK_ENCODING_AES_KEY": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
        "AGENTSEEK_WECOM_APP_ALLOWED_DIGITAL_EMPLOYEE_IDS": "industry-report,finance-assistant",
        "AGENTSEEK_WECOM_APP_DEFAULT_DIGITAL_EMPLOYEE_ID": "industry-report",
    })

    assert failures == []
    assert warnings == []


def test_transport_preflight_rejects_unsafe_application_configuration() -> None:
    failures, _ = _check_transport({
        "AGENTSEEK_WECOM_TRANSPORT_MODE": "callback",
        "AGENTSEEK_WECOM_CALLBACK_PATH": "/shared/callback",
        "AGENTSEEK_WECOM_TOKEN": "bot-token",
        "AGENTSEEK_WECOM_ENCODING_AES_KEY": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
        "AGENTSEEK_WECOM_CORP_ID": "corp-1",
        "AGENTSEEK_WECOM_APP_SECRET": "identity-helper-secret",
        "AGENTSEEK_WECOM_APP_TRANSPORT_ENABLED": "true",
        "AGENTSEEK_WECOM_DURABLE_MODE": "memory",
        "AGENTSEEK_WECOM_APP_AGENT_ID": "not-an-agent",
        "AGENTSEEK_WECOM_APP_TRANSPORT_SECRET": "application-secret",
        "AGENTSEEK_WECOM_APP_CALLBACK_PATH": "/shared/callback",
        "AGENTSEEK_WECOM_APP_CALLBACK_TOKEN": "invalid-token!",
        "AGENTSEEK_WECOM_APP_CALLBACK_ENCODING_AES_KEY": "too-short",
        "AGENTSEEK_WECOM_APP_ALLOWED_DIGITAL_EMPLOYEE_IDS": "industry-report",
        "AGENTSEEK_WECOM_APP_DEFAULT_DIGITAL_EMPLOYEE_ID": "finance-assistant",
    })

    assert "AGENTSEEK_WECOM_APP_AGENT_ID must be a positive integer" in failures
    assert "WeCom application and AI Bot callback paths must be distinct" in failures
    assert "AGENTSEEK_WECOM_APP_CALLBACK_TOKEN must contain 1 to 32 alphanumeric characters" in failures
    assert "AGENTSEEK_WECOM_APP_CALLBACK_ENCODING_AES_KEY must contain 43 characters" in failures
    assert "AGENTSEEK_WECOM_APP_DEFAULT_DIGITAL_EMPLOYEE_ID must be allowlisted" in failures
    assert "WeCom application production deployment requires AGENTSEEK_WECOM_DURABLE_MODE=sqlite" in failures


def test_outbound_preflight_rejects_unimplemented_long_connection_direct_file() -> None:
    failures, _ = _check({
        "AGENTSEEK_WECOM_TRANSPORT_MODE": "long_connection",
        "AGENTSEEK_WECOM_DURABLE_MODE": "sqlite",
        "AGENTSEEK_WORK_ARTIFACT_DELIVERY_MODE": "direct_file",
    })

    assert failures == ["long-connection media upload is not implemented in M0.5; use signed_link or disabled"]


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
