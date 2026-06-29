"""Trusted static assets exposed to an isolated DeepAgents runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

STATIC_AGENT_INSTRUCTIONS_PATH = "/assets/AGENTS.md"
STATIC_SKILLS_ROOT = "/skills"
MAX_STATIC_ASSET_BYTES = 1_048_576


@dataclass(frozen=True)
class StaticAgentAssets:
    """Read-only deployment assets copied into a StateBackend per invocation."""

    agent_instructions: str
    files: Mapping[str, Mapping[str, str]]

    def files_for_invocation(self) -> dict[str, dict[str, str]]:
        """Return an independent state payload for one agent invocation."""
        return {path: dict(file_data) for path, file_data in self.files.items()}


def load_static_agent_assets(project_root: Path) -> StaticAgentAssets:
    """Load only declared text assets, never the project's whole filesystem.

    ``AGENTS.md`` and ``skills/`` are deployment-owned assets. They are copied
    into LangGraph state so DeepAgents can read them through StateBackend
    without receiving access to the project directory, environment files, or
    any other host path.
    """
    root = project_root.resolve()
    instructions_path = root / "AGENTS.md"
    skills_root = root / "skills"

    if not instructions_path.is_file() or instructions_path.is_symlink():
        raise RuntimeError(f"Trusted agent instructions are missing or unsafe: {instructions_path}")
    if not skills_root.is_dir() or skills_root.is_symlink():
        raise RuntimeError(f"Trusted skill directory is missing or unsafe: {skills_root}")

    files: dict[str, Mapping[str, str]] = {
        STATIC_AGENT_INSTRUCTIONS_PATH: _text_file_data(instructions_path),
    }
    resolved_skills_root = skills_root.resolve()
    for source_path in sorted(skills_root.rglob("*")):
        if not source_path.is_file():
            continue
        if source_path.is_symlink():
            raise RuntimeError(f"Trusted skill asset must not be a symlink: {source_path}")
        resolved_source = source_path.resolve()
        try:
            relative_path = resolved_source.relative_to(resolved_skills_root)
        except ValueError as exc:
            raise RuntimeError(f"Trusted skill asset escapes its root: {source_path}") from exc
        if any(part.startswith(".") for part in relative_path.parts):
            raise RuntimeError(f"Trusted skill asset must not be hidden: {source_path}")
        virtual_path = f"{STATIC_SKILLS_ROOT}/{relative_path.as_posix()}"
        files[virtual_path] = _text_file_data(source_path)

    instructions = files[STATIC_AGENT_INSTRUCTIONS_PATH]["content"]
    return StaticAgentAssets(
        agent_instructions=instructions,
        files=MappingProxyType({path: MappingProxyType(dict(file_data)) for path, file_data in files.items()}),
    )


def _text_file_data(path: Path) -> Mapping[str, str]:
    size = path.stat().st_size
    if size > MAX_STATIC_ASSET_BYTES:
        raise RuntimeError(f"Trusted static asset exceeds {MAX_STATIC_ASSET_BYTES} bytes: {path}")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Trusted static asset must be UTF-8 text: {path}") from exc
    return {"content": content, "encoding": "utf-8"}
