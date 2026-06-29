from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import DotEnvSettingsSource, EnvSettingsSource

AGENTSEEK_ENV_PREFIX = "AGENTSEEK_"
BUB_ENV_PREFIX = "BUB_"
AGENTSEEK_ENV_FILE = "AGENTSEEK_ENV_FILE"

# Default layout when ``BUB_HOME`` / ``AGENTSEEK_HOME`` are unset: ``cwd / DEFAULT_AGENTSEEK_HOME``.
DEFAULT_AGENTSEEK_HOME = ".agentseek"

# Basename of Bub config under ``BUB_HOME``.
DEFAULT_AGENTSEEK_CONFIG = "config.yml"

# Under ``BUB_HOME`` when ``BUB_PROJECT`` / ``AGENTSEEK_PROJECT`` are unset.
DEFAULT_PLUGIN_SANDBOX = "agentseek-project"


class _AgentseekAliasProbeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=True,
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )


class AgentseekSettings(BaseSettings):
    """Runtime knobs resolved from the AGENTSEEK_* namespace."""

    model_config = SettingsConfigDict(
        env_prefix=AGENTSEEK_ENV_PREFIX,
        case_sensitive=False,
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # Enable local Logfire console rendering for spans/events.
    console: bool = Field(default=False)


def get_agentseek_settings() -> AgentseekSettings:
    """Resolve agentseek settings from the current process environment."""
    return AgentseekSettings()


def apply_agentseek_env_aliases(environ: MutableMapping[str, str] | None = None) -> None:
    """Let AGENTSEEK_* variables act as fallbacks for BUB_* variables.

    If ``AGENTSEEK_ENV_FILE`` is set in the process environment or in the
    current working directory's ``.env``, that dotenv file is loaded into the
    process environment before aliasing. This lets generated projects keep
    their runtime secrets in their own local dotenv file while still starting
    AgentSeek from the repository root.

    Also applies agentseek defaults for ``BUB_HOME`` (see ``DEFAULT_AGENTSEEK_HOME``) and
    ``BUB_PROJECT`` (under that home, see ``DEFAULT_PLUGIN_SANDBOX``) when unset.
    """
    target_environ = os.environ if environ is None else environ
    _load_agentseek_project_env_file(target_environ)
    for name, value in _collect_agentseek_bub_aliases(target_environ).items():
        target_environ.setdefault(name, value)
    _apply_agentseek_bub_location_defaults(target_environ)


def _apply_agentseek_bub_location_defaults(target_environ: MutableMapping[str, str]) -> None:
    """Set ``BUB_HOME`` then ``BUB_PROJECT`` only when missing (``setdefault``)."""
    target_environ.setdefault("BUB_HOME", _default_bub_home_for_agentseek())
    bub_home = Path(target_environ["BUB_HOME"]).expanduser()
    plugin_root = bub_home / DEFAULT_PLUGIN_SANDBOX
    target_environ.setdefault("BUB_PROJECT", str(plugin_root))


def _default_bub_home_for_agentseek() -> str:
    """String path Bub uses as ``BUB_HOME`` when the user has not set ``BUB_HOME`` / ``AGENTSEEK_HOME``."""
    return str(default_agentseek_home())


def agentseek_config_file() -> Path:
    bub_home = Path(os.environ["BUB_HOME"]).expanduser()
    return (bub_home / DEFAULT_AGENTSEEK_CONFIG).resolve()


def default_agentseek_home() -> Path:
    """Resolved directory for Bub runtime home when ``BUB_HOME`` is unset."""
    return Path.cwd() / DEFAULT_AGENTSEEK_HOME


def _collect_agentseek_bub_aliases(target_environ: Mapping[str, str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for env_vars in _iter_agentseek_env_vars(target_environ):
        aliases.update(_bub_aliases(env_vars))
    return aliases


def _iter_agentseek_env_vars(target_environ: Mapping[str, str]) -> tuple[Mapping[str, str | None], ...]:
    return (
        DotEnvSettingsSource(_AgentseekAliasProbeSettings).env_vars,
        EnvSettingsSource(_AgentseekAliasProbeSettings).env_vars,
        target_environ,
    )


def _bub_aliases(env_vars: Mapping[str, str | None]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for name, value in env_vars.items():
        if not name.startswith(AGENTSEEK_ENV_PREFIX) or value is None:
            continue

        suffix = name.removeprefix(AGENTSEEK_ENV_PREFIX)
        if suffix:
            aliases[f"{BUB_ENV_PREFIX}{suffix}"] = value
    return aliases


def _load_agentseek_project_env_file(target_environ: MutableMapping[str, str]) -> None:
    env_file = _project_env_file_value(target_environ)
    if not env_file:
        return

    path = Path(env_file).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    for key, value in _read_dotenv_values(path).items():
        if value != "":
            target_environ.setdefault(key, value)


def _project_env_file_value(target_environ: Mapping[str, str]) -> str:
    explicit = target_environ.get(AGENTSEEK_ENV_FILE) or os.environ.get(AGENTSEEK_ENV_FILE)
    if explicit:
        return explicit.strip()
    return _read_dotenv_values(Path.cwd() / ".env").get(AGENTSEEK_ENV_FILE, "").strip()


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
