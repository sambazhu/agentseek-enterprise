from __future__ import annotations

import os
import zipfile
from pathlib import Path
from typing import Any


def connect(user: str, password: str, server: str, port: int, **_: Any) -> Any:
    """DB-API compatible connect function backed by JayDeBeApi."""
    java_home = os.environ.get("AGENTSEEK_IDENTITY_DM_JDBC_JAVA_HOME", "").strip()
    if java_home:
        os.environ.setdefault("JAVA_HOME", java_home)

    try:
        import jaydebeapi
    except ModuleNotFoundError as exc:
        msg = (
            "Missing optional JDBC bridge dependency 'jaydebeapi'. "
            "Install jaydebeapi and JPype1, or switch AGENTSEEK_IDENTITY_DM_DRIVER_MODULE to dmPython."
        )
        raise RuntimeError(msg) from exc

    jdbc_class = os.environ.get("AGENTSEEK_IDENTITY_DM_JDBC_CLASS", "dm.jdbc.driver.DmDriver")
    jdbc_url = os.environ.get("AGENTSEEK_IDENTITY_DM_JDBC_URL", f"jdbc:dm://{server}:{port}")
    jar_path = _resolve_jdbc_jar()
    return jaydebeapi.connect(jdbc_class, jdbc_url, [user, password], str(jar_path))


def _resolve_jdbc_jar() -> Path:
    explicit_jar = os.environ.get("AGENTSEEK_IDENTITY_DM_JDBC_JAR", "").strip()
    if explicit_jar:
        jar_path = Path(explicit_jar).expanduser().resolve()
        if not jar_path.is_file():
            msg = f"AGENTSEEK_IDENTITY_DM_JDBC_JAR does not exist: {jar_path}"
            raise RuntimeError(msg)
        return jar_path

    boot_jar = os.environ.get("AGENTSEEK_IDENTITY_DM_JDBC_BOOT_JAR", "").strip()
    if not boot_jar:
        msg = "Set AGENTSEEK_IDENTITY_DM_JDBC_JAR or AGENTSEEK_IDENTITY_DM_JDBC_BOOT_JAR."
        raise RuntimeError(msg)

    boot_jar_path = Path(boot_jar).expanduser().resolve()
    nested_name = os.environ.get(
        "AGENTSEEK_IDENTITY_DM_JDBC_NESTED_JAR",
        "BOOT-INF/lib/DmJdbcDriver18-8.1.2.192.jar",
    )
    if not boot_jar_path.is_file():
        msg = f"AGENTSEEK_IDENTITY_DM_JDBC_BOOT_JAR does not exist: {boot_jar_path}"
        raise RuntimeError(msg)

    cache_dir = Path(os.environ.get("AGENTSEEK_IDENTITY_DM_JDBC_CACHE_DIR", "/private/tmp/agentseek-identity-jdbc"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    output_path = cache_dir / Path(nested_name).name
    if output_path.is_file():
        return output_path

    with zipfile.ZipFile(boot_jar_path) as archive:
        try:
            with archive.open(nested_name) as source, output_path.open("wb") as target:
                target.write(source.read())
        except KeyError as exc:
            msg = f"Nested JDBC jar not found in {boot_jar_path}: {nested_name}"
            raise RuntimeError(msg) from exc
    return output_path
