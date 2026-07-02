#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import os
import secrets
from pathlib import Path


TRUE_VALUES = {"1", "true", "yes", "on"}
PLACEHOLDER_MARKERS = ("<", ">", "changeme", "replace-me", "your-")


class CheckReport:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def ok(self, message: str) -> None:
        print(f"OK   {message}")

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        print(f"WARN {message}")

    def fail(self, message: str) -> None:
        self.failures.append(message)
        print(f"FAIL {message}")

    def exit_code(self, *, strict: bool) -> int:
        if self.failures or (strict and self.warnings):
            return 1
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Production preflight for the Enterprise WeCom gateway.")
    parser.add_argument("--env-file", default=os.environ.get("AGENTSEEK_ENV_FILE", ".env"))
    parser.add_argument("--strict", action="store_true", help="Treat warnings as failures.")
    parser.add_argument(
        "--generate-namespace-secret",
        action="store_true",
        help="Print a new high-entropy AGENTSEEK_ENTERPRISE_NAMESPACE_SECRET value and exit.",
    )
    args = parser.parse_args(argv)

    if args.generate_namespace_secret:
        print(generate_namespace_secret())
        return 0

    project_root = Path.cwd()
    env_path = Path(args.env_file)
    if not env_path.is_absolute():
        env_path = project_root / env_path

    report = CheckReport()
    env = load_env_file(env_path, report)
    if env is None:
        return 1

    check_model(env, report)
    check_wecom(env, report)
    check_identity(env, project_root, report)
    check_memory(env, project_root, report)
    check_contextseek(env, project_root, report)
    check_mcp(env, project_root, report)
    check_tracing(env, report)
    check_launchd(project_root, env_path.parent, report)

    if report.failures:
        print(f"\nProduction preflight failed: {len(report.failures)} failure(s), {len(report.warnings)} warning(s).")
    elif report.warnings:
        print(f"\nProduction preflight passed with {len(report.warnings)} warning(s).")
    else:
        print("\nProduction preflight passed.")
    return report.exit_code(strict=args.strict)


def generate_namespace_secret() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def load_env_file(path: Path, report: CheckReport) -> dict[str, str] | None:
    if not path.is_file():
        report.fail(f"env file not found: {path}")
        return None
    env: dict[str, str] = dict(os.environ)
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env[key.strip()] = _unquote(value.strip())
    report.ok(f"env file found: {path}")
    return env


def check_model(env: dict[str, str], report: CheckReport) -> None:
    require(env, report, "AGENTSEEK_MODEL")
    provider = env.get("AGENTSEEK_MODEL_PROVIDER", "").strip().lower()
    if provider == "openai":
        require_any(env, report, ("AGENTSEEK_API_KEY", "OPENAI_API_KEY"), "OpenAI-compatible API key")
    elif provider:
        report.warn(f"model provider {provider!r} is not explicitly checked by this preflight")
    else:
        report.warn("AGENTSEEK_MODEL_PROVIDER is not set")


def check_wecom(env: dict[str, str], report: CheckReport) -> None:
    for key in (
        "AGENTSEEK_WECOM_CALLBACK_PATH",
        "AGENTSEEK_WECOM_TOKEN",
        "AGENTSEEK_WECOM_ENCODING_AES_KEY",
        "AGENTSEEK_WECOM_CORP_ID",
        "AGENTSEEK_WECOM_APP_SECRET",
    ):
        require(env, report, key)
    if env.get("AGENTSEEK_WECOM_USERID_RESOLVE_MODE") != "openuserid_to_userid":
        report.warn("AGENTSEEK_WECOM_USERID_RESOLVE_MODE should usually be openuserid_to_userid")


def check_identity(env: dict[str, str], project_root: Path, report: CheckReport) -> None:
    if env.get("AGENTSEEK_IDENTITY_PROVIDER") != "dm":
        report.fail("AGENTSEEK_IDENTITY_PROVIDER must be dm for employee identity resolution")
    for key in (
        "AGENTSEEK_IDENTITY_DM_HOST",
        "AGENTSEEK_IDENTITY_DM_USER",
        "AGENTSEEK_IDENTITY_DM_PASSWORD",
        "AGENTSEEK_IDENTITY_DM_SCHEMA",
        "AGENTSEEK_IDENTITY_DM_DRIVER_MODULE",
    ):
        require(env, report, key)

    mode = env.get("AGENTSEEK_IDENTITY_DM_EXECUTION_MODE", "in_process").strip()
    if mode in {"subprocess", "sidecar"}:
        report.ok(f"DM execution mode keeps JVM out of gateway: {mode}")
    else:
        report.fail("AGENTSEEK_IDENTITY_DM_EXECUTION_MODE should be subprocess or sidecar in production")

    if env.get("AGENTSEEK_IDENTITY_DM_DRIVER_MODULE") == "agentseek_enterprise.identity.jdbc_driver":
        check_existing_path(env, project_root, report, "AGENTSEEK_IDENTITY_DM_JDBC_JAR", file_expected=True)
        check_existing_path(env, project_root, report, "AGENTSEEK_IDENTITY_DM_JDBC_JAVA_HOME", file_expected=False)

    if truthy(env.get("AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_ENABLED")):
        report.ok("employee identity cache enabled")
    else:
        report.warn("employee identity cache is disabled")


def check_memory(env: dict[str, str], project_root: Path, report: CheckReport) -> None:
    secret = env.get("AGENTSEEK_ENTERPRISE_NAMESPACE_SECRET", "").strip()
    if len(secret) >= 32 and not contains_placeholder(secret):
        report.ok("enterprise namespace secret is set")
    else:
        report.fail("AGENTSEEK_ENTERPRISE_NAMESPACE_SECRET must be a high-entropy value before production")
    if env.get("AGENTSEEK_ENTERPRISE_MEMORY_SQLALCHEMY_URL", "").strip():
        report.ok("short-term memory uses SQLAlchemy URL")
        warn_if_placeholder_password(env["AGENTSEEK_ENTERPRISE_MEMORY_SQLALCHEMY_URL"], report, "short-term memory")
    else:
        ensure_parent_writable(env, project_root, report, "AGENTSEEK_ENTERPRISE_MEMORY_SQLITE_PATH")
    if env.get("AGENTSEEK_ENTERPRISE_STORE_SQLALCHEMY_URL", "").strip():
        report.ok("explicit durable memory uses SQLAlchemy URL")
        warn_if_placeholder_password(env["AGENTSEEK_ENTERPRISE_STORE_SQLALCHEMY_URL"], report, "explicit durable memory")
    else:
        require(env, report, "AGENTSEEK_ENTERPRISE_STORE_SQLITE_PATH")
        ensure_parent_writable(env, project_root, report, "AGENTSEEK_ENTERPRISE_STORE_SQLITE_PATH")


def check_contextseek(env: dict[str, str], project_root: Path, report: CheckReport) -> None:
    backend = env.get("AGENTSEEK_CTX_STORAGE_BACKEND", "").strip()
    if backend == "seekdb":
        report.ok("ContextSeek storage backend is seekdb")
        ensure_parent_writable(env, project_root, report, "AGENTSEEK_CTX_SEEKDB_PATH", path_is_directory=True)
    elif backend == "pgvector":
        report.ok("ContextSeek storage backend is pgvector")
        require(env, report, "AGENTSEEK_CTX_PGVECTOR_URL")
        warn_if_placeholder_password(env.get("AGENTSEEK_CTX_PGVECTOR_URL", ""), report, "ContextSeek pgvector")
        table = env.get("AGENTSEEK_CTX_PGVECTOR_TABLE", "").strip()
        if table:
            report.ok("AGENTSEEK_CTX_PGVECTOR_TABLE is set")
        else:
            report.warn("AGENTSEEK_CTX_PGVECTOR_TABLE is empty; default contextseek_pgvector_items will be used")
        dims = env.get("AGENTSEEK_CTX_PGVECTOR_DIMS", "1024").strip()
        if dims == "1024":
            report.ok("pgvector embedding dims are 1024 for bge-m3 dense")
        else:
            report.fail(f"AGENTSEEK_CTX_PGVECTOR_DIMS should be 1024 for bge-m3 dense, got {dims!r}")
        check_existing_path(env, project_root, report, "AGENTSEEK_CTX_BGE_M3_ONNX_MODEL_PATH", file_expected=True)
        check_existing_path(env, project_root, report, "AGENTSEEK_CTX_BGE_M3_TOKENIZER_PATH", file_expected=True)
    else:
        report.warn(f"ContextSeek storage backend is {backend!r}; seekdb and pgvector are the verified persistent modes")


def check_mcp(env: dict[str, str], project_root: Path, report: CheckReport) -> None:
    check_existing_path(env, project_root, report, "AGENTSEEK_MCP_CONFIG_PATH", file_expected=True)


def check_tracing(env: dict[str, str], report: CheckReport) -> None:
    tracing = truthy(env.get("LANGSMITH_TRACING"))
    if not tracing:
        report.ok("LangSmith tracing disabled")
        return
    if truthy(env.get("AGENTSEEK_PRODUCTION_TRACING_ACK")):
        report.ok("LangSmith tracing enabled with production acknowledgement")
    else:
        report.warn("LANGSMITH_TRACING=true; set AGENTSEEK_PRODUCTION_TRACING_ACK=true if this is intentional")


def check_launchd(project_root: Path, env_dir: Path, report: CheckReport) -> None:
    launchd_dirs = [project_root / "launchd"]
    env_launchd_dir = env_dir / "launchd"
    if env_launchd_dir not in launchd_dirs:
        launchd_dirs.append(env_launchd_dir)
    plists = [plist for directory in launchd_dirs if directory.is_dir() for plist in sorted(directory.glob("*.plist"))]
    if not plists:
        report.warn("no launchd plist found")
        return
    report.ok(f"launchd plist available: {', '.join(path.name for path in plists)}")


def require(env: dict[str, str], report: CheckReport, key: str) -> None:
    value = env.get(key, "").strip()
    if value and not contains_placeholder(value):
        report.ok(f"{key} is set")
    else:
        report.fail(f"{key} is missing or still a placeholder")


def require_any(env: dict[str, str], report: CheckReport, keys: tuple[str, ...], label: str) -> None:
    if any(env.get(key, "").strip() and not contains_placeholder(env.get(key, "")) for key in keys):
        report.ok(f"{label} is set")
    else:
        report.fail(f"{label} is missing")


def check_existing_path(
    env: dict[str, str],
    project_root: Path,
    report: CheckReport,
    key: str,
    *,
    file_expected: bool,
) -> None:
    value = env.get(key, "").strip()
    if not value or contains_placeholder(value):
        report.fail(f"{key} is missing or still a placeholder")
        return
    path = resolve_path(project_root, value)
    exists = path.is_file() if file_expected else path.exists()
    if exists:
        report.ok(f"{key} exists")
    else:
        report.fail(f"{key} does not exist: {path}")


def ensure_parent_writable(
    env: dict[str, str],
    project_root: Path,
    report: CheckReport,
    key: str,
    *,
    path_is_directory: bool = False,
) -> None:
    value = env.get(key, "").strip()
    if not value or contains_placeholder(value):
        report.fail(f"{key} is missing or still a placeholder")
        return
    path = resolve_path(project_root, value)
    target = path if path_is_directory else path.parent
    target.mkdir(parents=True, exist_ok=True)
    if os.access(target, os.W_OK):
        report.ok(f"{key} parent is writable")
    else:
        report.fail(f"{key} parent is not writable: {target}")


def resolve_path(project_root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return project_root / path


def contains_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return any(marker in normalized for marker in PLACEHOLDER_MARKERS)


def truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def warn_if_placeholder_password(url: str, report: CheckReport, label: str) -> None:
    normalized = str(url or "").lower()
    if "<password>" in normalized or ":password@" in normalized or ":pass@" in normalized:
        report.fail(f"{label} SQLAlchemy URL still contains a placeholder password")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
