"""Start Bub while making optional Logfire instrumentation non-fatal.

Bub 0.3.9 configures Logfire at import time. When logfire is installed but no
token is configured, that import can fail before the gateway starts. The
enterprise gateway treats Logfire as optional, so this wrapper downgrades that
case to local-only instrumentation.
"""

from __future__ import annotations

import os
from pathlib import Path


def _load_project_env_file() -> None:
    env_file = os.environ.get("AGENTSEEK_ENV_FILE", "").strip()
    if not env_file:
        return

    path = Path(env_file).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path

    for key, value in _read_dotenv_values(path).items():
        os.environ[key] = value


def _read_dotenv_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _apply_project_bub_aliases() -> None:
    for key, value in list(os.environ.items()):
        if not key.startswith("AGENTSEEK_"):
            continue
        suffix = key.removeprefix("AGENTSEEK_")
        if suffix:
            os.environ[f"BUB_{suffix}"] = value


def _guard_logfire_configure() -> None:
    try:
        import logfire
        from logfire.exceptions import LogfireConfigError
    except ImportError:
        return

    original_configure = logfire.configure

    def configure_without_required_token(*args: object, **kwargs: object) -> object:
        try:
            return original_configure(*args, **kwargs)
        except LogfireConfigError:
            fallback_kwargs = dict(kwargs)
            fallback_kwargs["send_to_logfire"] = False
            return original_configure(*args, **fallback_kwargs)

    logfire.configure = configure_without_required_token


def _initialize_signed_link_delivery() -> None:
    if os.environ.get("AGENTSEEK_WORK_ENABLED", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    if os.environ.get("AGENTSEEK_WORK_ARTIFACT_DELIVERY_MODE", "disabled").strip() != "signed_link":
        return
    from enterprise_wecom_digital_employee.work_composition import get_work_composition

    get_work_composition()


def main() -> None:
    _load_project_env_file()
    _apply_project_bub_aliases()
    from agentseek.env import apply_agentseek_env_aliases

    apply_agentseek_env_aliases()
    _guard_logfire_configure()
    _initialize_signed_link_delivery()

    from bub.__main__ import app

    app()


if __name__ == "__main__":
    main()
