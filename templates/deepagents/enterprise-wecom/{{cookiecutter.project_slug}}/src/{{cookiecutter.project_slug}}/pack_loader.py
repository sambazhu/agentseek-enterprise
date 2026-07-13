from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import mkdtemp
from types import MappingProxyType
from typing import Any

import yaml
from agentseek_work import PackSnapshot


class PackLoadError(RuntimeError):
    """Raised when a role pack violates its frozen manifest or trust boundary."""


@dataclass(frozen=True, slots=True)
class SkillSpec:
    skill_id: str
    version: str
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class PlaybookSpec:
    playbook_id: str
    version: str
    entrypoint: str


@dataclass(frozen=True, slots=True)
class AssetSpec:
    asset_id: str
    version: str
    artifact_ref: str
    sha256: str


@dataclass(frozen=True, slots=True)
class DigitalEmployeeProfile:
    digital_employee_id: str
    tenant_id: str
    name: str
    owning_org: str
    job_role: str
    responsibilities: tuple[str, ...]
    pack_id: str
    pack_version: str
    supported_playbooks: tuple[str, ...]
    skill_refs: tuple[str, ...]
    asset_refs: tuple[str, ...]
    tool_grants: tuple[str, ...]
    data_scopes: tuple[str, ...]
    requester_scope: tuple[str, ...]
    escalation_policy: Mapping[str, Any]
    service_status: str
    profile_version: str


@dataclass(frozen=True, slots=True)
class PackContentFile:
    relative_path: str
    content: bytes


@dataclass(frozen=True, slots=True)
class LoadedPackManifest:
    pack_root: Path
    schema_version: int
    pack_id: str
    pack_version: str
    manifest_digest: str
    content_digest: str
    profile: DigitalEmployeeProfile
    skills: tuple[SkillSpec, ...]
    playbooks: tuple[PlaybookSpec, ...]
    assets: tuple[AssetSpec, ...]
    policies: tuple[str, ...]
    eval_manifest: str
    content_files: tuple[PackContentFile, ...]

    @property
    def skill_digests(self) -> tuple[str, ...]:
        return tuple(f"sha256:{skill.sha256}" for skill in self.skills)


class RestrictedPackLoader:
    def __init__(
        self,
        *,
        pack_root: Path,
        allowed_entrypoint_package: str,
        asset_resolver: Callable[[str], Path],
    ) -> None:
        if pack_root.is_symlink():
            raise PackLoadError("pack root must not be a symlink")
        self._pack_root = pack_root.resolve(strict=True)
        self._allowed_entrypoint_package = allowed_entrypoint_package.strip()
        self._asset_resolver = asset_resolver
        if not self._allowed_entrypoint_package:
            raise ValueError("allowed_entrypoint_package must not be blank")

    def load(self) -> LoadedPackManifest:
        manifest_path = self._local_file("pack.yaml")
        manifest_content = manifest_path.read_bytes()
        manifest = _yaml_mapping(manifest_content, "pack.yaml")
        if _required_int(manifest, "schema_version") != 1:
            raise PackLoadError("unsupported pack schema_version")
        pack_id = _required_text(manifest, "pack_id")
        pack_version = _required_text(manifest, "pack_version")
        profile_path = _required_text(manifest, "profile")
        skills = self._load_skills(manifest.get("skills", []))
        playbooks = self._load_playbooks(manifest.get("playbooks", []))
        assets = self._load_assets(manifest.get("assets", []))
        policies = self._load_policy_paths(manifest.get("policies", []))
        eval_manifest = self._load_eval_manifest(manifest.get("evals"))
        profile = self._load_profile(profile_path, pack_id=pack_id, pack_version=pack_version)
        _validate_profile_refs(profile, skills=skills, playbooks=playbooks, assets=assets)

        content_paths = {"pack.yaml", profile_path, *policies, eval_manifest}
        for skill in skills:
            content_paths.update(self._skill_content_paths(skill.path))
        content_files = tuple(
            PackContentFile(relative_path=path, content=self._local_file(path).read_bytes())
            for path in sorted(content_paths)
        )
        return LoadedPackManifest(
            pack_root=self._pack_root,
            schema_version=1,
            pack_id=pack_id,
            pack_version=pack_version,
            manifest_digest=sha256(manifest_content).hexdigest(),
            content_digest=_content_digest(content_files),
            profile=profile,
            skills=skills,
            playbooks=playbooks,
            assets=assets,
            policies=policies,
            eval_manifest=eval_manifest,
            content_files=content_files,
        )

    def _load_skills(self, value: Any) -> tuple[SkillSpec, ...]:
        entries = _mapping_sequence(value, "skills")
        skills: list[SkillSpec] = []
        for entry in entries:
            path = _required_text(entry, "path")
            content = self._local_file(path).read_bytes()
            _require_utf8(content, path)
            declared_digest = _sha256_text(entry, "sha256")
            _require_digest(content, declared_digest, path)
            skills.append(
                SkillSpec(
                    skill_id=_required_text(entry, "id"),
                    version=_required_text(entry, "version"),
                    path=path,
                    sha256=declared_digest,
                )
            )
        _require_unique((skill.skill_id for skill in skills), "skill id")
        return tuple(skills)

    def _load_playbooks(self, value: Any) -> tuple[PlaybookSpec, ...]:
        entries = _mapping_sequence(value, "playbooks")
        playbooks: list[PlaybookSpec] = []
        for entry in entries:
            entrypoint = _required_text(entry, "entrypoint")
            _validate_entrypoint(entrypoint, self._allowed_entrypoint_package)
            playbooks.append(
                PlaybookSpec(
                    playbook_id=_required_text(entry, "id"),
                    version=_required_text(entry, "version"),
                    entrypoint=entrypoint,
                )
            )
        _require_unique((playbook.playbook_id for playbook in playbooks), "playbook id")
        return tuple(playbooks)

    def _load_assets(self, value: Any) -> tuple[AssetSpec, ...]:
        entries = _mapping_sequence(value, "assets")
        assets: list[AssetSpec] = []
        for entry in entries:
            artifact_ref = _required_text(entry, "artifact_ref")
            declared_digest = _sha256_text(entry, "sha256")
            asset_path = self._asset_resolver(artifact_ref)
            if asset_path.is_symlink() or not asset_path.is_file():
                raise PackLoadError(f"trusted asset is not a regular file: {artifact_ref}")
            _require_digest(asset_path.read_bytes(), declared_digest, artifact_ref)
            assets.append(
                AssetSpec(
                    asset_id=_required_text(entry, "id"),
                    version=_required_text(entry, "version"),
                    artifact_ref=artifact_ref,
                    sha256=declared_digest,
                )
            )
        _require_unique((asset.asset_id for asset in assets), "asset id")
        return tuple(assets)

    def _load_policy_paths(self, value: Any) -> tuple[str, ...]:
        paths = _text_sequence(value, "policies")
        for path in paths:
            _require_utf8(self._local_file(path).read_bytes(), path)
        _require_unique(paths, "policy path")
        return paths

    def _load_eval_manifest(self, value: Any) -> str:
        mapping = _require_mapping(value, "evals")
        path = _required_text(mapping, "manifest")
        _yaml_mapping(self._local_file(path).read_bytes(), path)
        return path

    def _load_profile(
        self,
        path: str,
        *,
        pack_id: str,
        pack_version: str,
    ) -> DigitalEmployeeProfile:
        profile = _yaml_mapping(self._local_file(path).read_bytes(), path)
        loaded = DigitalEmployeeProfile(
            digital_employee_id=_required_text(profile, "digital_employee_id"),
            tenant_id=_required_text(profile, "tenant_id"),
            name=_required_text(profile, "name"),
            owning_org=_required_text(profile, "owning_org"),
            job_role=_required_text(profile, "job_role"),
            responsibilities=_text_sequence(profile.get("responsibilities"), "responsibilities"),
            pack_id=_required_text(profile, "pack_id"),
            pack_version=_required_text(profile, "pack_version"),
            supported_playbooks=_text_sequence(profile.get("supported_playbooks"), "supported_playbooks"),
            skill_refs=_text_sequence(profile.get("skill_refs"), "skill_refs"),
            asset_refs=_text_sequence(profile.get("asset_refs"), "asset_refs"),
            tool_grants=_text_sequence(profile.get("tool_grants"), "tool_grants"),
            data_scopes=_text_sequence(profile.get("data_scopes"), "data_scopes"),
            requester_scope=_text_sequence(profile.get("requester_scope"), "requester_scope"),
            escalation_policy=MappingProxyType(
                dict(_require_mapping(profile.get("escalation_policy"), "escalation_policy"))
            ),
            service_status=_required_text(profile, "service_status"),
            profile_version=_required_text(profile, "profile_version"),
        )
        if loaded.pack_id != pack_id or loaded.pack_version != pack_version:
            raise PackLoadError("profile pack id/version does not match pack.yaml")
        return loaded

    def _skill_content_paths(self, skill_path: str) -> set[str]:
        skill_root = PurePosixPath(skill_path).parent.as_posix()
        root = self._local_directory(skill_root)
        paths: set[str] = set()
        for candidate in root.rglob("*"):
            relative = candidate.relative_to(self._pack_root)
            _validate_relative_parts(relative.parts, relative.as_posix())
            if candidate.is_symlink():
                raise PackLoadError(f"symlink is not allowed in pack content: {relative.as_posix()}")
            if candidate.is_file():
                content = candidate.read_bytes()
                _require_utf8(content, relative.as_posix())
                paths.add(relative.as_posix())
        return paths

    def _local_file(self, raw_path: str) -> Path:
        candidate = self._local_path(raw_path)
        if not candidate.is_file():
            raise PackLoadError(f"pack file does not exist: {raw_path}")
        return candidate

    def _local_directory(self, raw_path: str) -> Path:
        candidate = self._local_path(raw_path)
        if not candidate.is_dir():
            raise PackLoadError(f"pack directory does not exist: {raw_path}")
        return candidate

    def _local_path(self, raw_path: str) -> Path:
        pure = PurePosixPath(raw_path)
        _validate_relative_parts(pure.parts, raw_path)
        candidate = self._pack_root.joinpath(*pure.parts)
        current = self._pack_root
        for part in pure.parts:
            current = current / part
            if current.is_symlink():
                raise PackLoadError(f"symlink is not allowed in pack path: {raw_path}")
        try:
            candidate.resolve(strict=True).relative_to(self._pack_root)
        except (FileNotFoundError, ValueError) as exc:
            raise PackLoadError(f"pack path escapes its root or does not exist: {raw_path}") from exc
        return candidate


class FilesystemPackSnapshotStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        if self._root.is_symlink():
            raise PackLoadError("snapshot store root must not be a symlink")
        self._root = self._root.resolve(strict=True)

    def put(self, loaded: LoadedPackManifest) -> str:
        artifact_id = f"pack-content://sha256/{loaded.content_digest}"
        target = self._root / loaded.content_digest
        if target.exists():
            _verify_snapshot_directory(target, loaded.content_files)
            return artifact_id
        temporary = Path(mkdtemp(prefix=".pack-", dir=self._root))
        try:
            for content_file in loaded.content_files:
                destination = temporary.joinpath(*PurePosixPath(content_file.relative_path).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content_file.content)
            try:
                os.replace(temporary, target)
            except OSError:
                if not target.exists():
                    raise
                _verify_snapshot_directory(target, loaded.content_files)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return artifact_id

    def resolve(self, content_artifact_id: str) -> Path:
        prefix = "pack-content://sha256/"
        if not content_artifact_id.startswith(prefix):
            raise PackLoadError("unsupported pack content artifact id")
        digest = content_artifact_id.removeprefix(prefix)
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise PackLoadError("invalid pack content digest")
        target = self._root / digest
        if not target.is_dir() or target.is_symlink():
            raise PackLoadError("pack content artifact is unavailable")
        files = _content_files_from_directory(target)
        if _content_digest(files) != digest:
            raise PackLoadError("pack content artifact digest does not match stored content")
        return target


def build_pack_snapshot(
    loaded: LoadedPackManifest,
    *,
    store: FilesystemPackSnapshotStore,
    created_at: datetime,
    source_repository: str | None = None,
    source_commit: str | None = None,
) -> PackSnapshot:
    content_artifact_id = store.put(loaded)
    return PackSnapshot(
        pack_snapshot_id=f"pack_snapshot_sha256_{loaded.content_digest}",
        pack_id=loaded.pack_id,
        pack_version=loaded.pack_version,
        source_repository=source_repository,
        source_commit=source_commit,
        manifest_digest=f"sha256:{loaded.manifest_digest}",
        content_artifact_id=content_artifact_id,
        asset_version_refs=tuple(
            f"{asset.asset_id}@{asset.version}:{asset.artifact_ref}#{asset.sha256}" for asset in loaded.assets
        ),
        created_at=created_at,
    )


def materialize_profile_skills(loaded: LoadedPackManifest, destination: Path) -> tuple[Path, ...]:
    if destination.exists() and any(destination.iterdir()):
        raise PackLoadError("skill destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise PackLoadError("skill destination must not be a symlink")
    selected: list[Path] = []
    content_by_path = {content.relative_path: content.content for content in loaded.content_files}
    skills = {f"{skill.skill_id}@{skill.version}": skill for skill in loaded.skills}
    for skill_ref in loaded.profile.skill_refs:
        skill = skills[skill_ref]
        skill_root = PurePosixPath(skill.path).parent
        target_root = destination / skill.skill_id
        for relative_path, content in content_by_path.items():
            relative = PurePosixPath(relative_path)
            try:
                child = relative.relative_to(skill_root)
            except ValueError:
                continue
            target = target_root.joinpath(*child.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        selected.append(target_root)
    return tuple(selected)


def _validate_profile_refs(
    profile: DigitalEmployeeProfile,
    *,
    skills: Sequence[SkillSpec],
    playbooks: Sequence[PlaybookSpec],
    assets: Sequence[AssetSpec],
) -> None:
    _require_declared_refs(
        profile.skill_refs,
        {f"{item.skill_id}@{item.version}" for item in skills},
        "skill_refs",
    )
    _require_declared_refs(
        profile.supported_playbooks,
        {f"{item.playbook_id}@{item.version}" for item in playbooks},
        "supported_playbooks",
    )
    _require_declared_refs(
        profile.asset_refs,
        {f"{item.asset_id}@{item.version}" for item in assets},
        "asset_refs",
    )


def _require_declared_refs(refs: Sequence[str], declared: set[str], field_name: str) -> None:
    _require_unique(refs, field_name)
    missing = sorted(set(refs) - declared)
    if missing:
        raise PackLoadError(f"{field_name} contains undeclared references: {', '.join(missing)}")


def _validate_entrypoint(entrypoint: str, allowed_package: str) -> None:
    module, separator, attribute = entrypoint.partition(":")
    if separator != ":" or not attribute.isidentifier():
        raise PackLoadError(f"invalid playbook entrypoint: {entrypoint}")
    if module != allowed_package and not module.startswith(f"{allowed_package}."):
        raise PackLoadError(f"playbook entrypoint is outside the allowed package: {entrypoint}")


def _validate_relative_parts(parts: Sequence[str], raw_path: str) -> None:
    if not parts or raw_path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise PackLoadError(f"pack path must be normalized and relative: {raw_path}")
    if any(part.startswith(".") for part in parts):
        raise PackLoadError(f"hidden pack path is not allowed: {raw_path}")


def _verify_snapshot_directory(target: Path, expected: Sequence[PackContentFile]) -> None:
    if any(path.is_symlink() for path in target.rglob("*")):
        raise PackLoadError("stored pack snapshot must not contain symlinks")
    actual_paths = {
        path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file() and not path.is_symlink()
    }
    expected_paths = {item.relative_path for item in expected}
    if actual_paths != expected_paths:
        raise PackLoadError("stored pack snapshot file set does not match its digest")
    expected_by_path = {item.relative_path: item.content for item in expected}
    for relative_path in actual_paths:
        if (target / relative_path).read_bytes() != expected_by_path[relative_path]:
            raise PackLoadError("stored pack snapshot content does not match its digest")


def _content_files_from_directory(root: Path) -> tuple[PackContentFile, ...]:
    files: list[PackContentFile] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise PackLoadError("stored pack snapshot must not contain symlinks")
        if path.is_file():
            files.append(
                PackContentFile(
                    relative_path=path.relative_to(root).as_posix(),
                    content=path.read_bytes(),
                )
            )
    return tuple(sorted(files, key=lambda item: item.relative_path))


def _content_digest(content_files: Sequence[PackContentFile]) -> str:
    digest = sha256()
    for content_file in content_files:
        path = content_file.relative_path.encode()
        digest.update(len(path).to_bytes(8, "big"))
        digest.update(path)
        digest.update(len(content_file.content).to_bytes(8, "big"))
        digest.update(content_file.content)
    return digest.hexdigest()


def _require_digest(content: bytes, declared_digest: str, label: str) -> None:
    if sha256(content).hexdigest() != declared_digest:
        raise PackLoadError(f"sha256 mismatch for {label}")


def _require_utf8(content: bytes, label: str) -> None:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackLoadError(f"pack text file must be UTF-8: {label}") from exc


def _yaml_mapping(content: bytes, label: str) -> Mapping[str, Any]:
    _require_utf8(content, label)
    try:
        value = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise PackLoadError(f"invalid YAML in {label}") from exc
    return _require_mapping(value, label)


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PackLoadError(f"{label} must be a mapping")
    return value


def _mapping_sequence(value: Any, label: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        raise PackLoadError(f"{label} must be a list")
    return tuple(_require_mapping(item, label) for item in value)


def _text_sequence(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise PackLoadError(f"{label} must be a non-empty list")
    values = tuple(str(item).strip() for item in value)
    if any(not item for item in values):
        raise PackLoadError(f"{label} must contain non-blank text")
    return values


def _required_text(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PackLoadError(f"{key} must be non-blank text")
    return value.strip()


def _required_int(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise PackLoadError(f"{key} must be an integer")
    return value


def _sha256_text(mapping: Mapping[str, Any], key: str) -> str:
    value = _required_text(mapping, key).lower()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise PackLoadError(f"{key} must be a lowercase sha256 hex digest")
    return value


def _require_unique(values: Sequence[str] | Any, label: str) -> None:
    materialized = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise PackLoadError(f"duplicate {label}")
