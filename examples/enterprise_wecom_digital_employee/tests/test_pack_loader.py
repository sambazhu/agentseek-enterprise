from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from enterprise_wecom_digital_employee.pack_loader import (
    FilesystemPackSnapshotStore,
    PackLoadError,
    RestrictedPackLoader,
    build_pack_snapshot,
    materialize_profile_skills,
)

PROJECT_ROOT = Path(__file__).parents[1]
SOURCE_PACK = PROJECT_ROOT / "digital_employees" / "industry-report"
ASSET_REF = "trusted-asset://strategic-report-docx/1.0.0"


def copy_pack(tmp_path: Path) -> Path:
    pack_root = tmp_path / "industry-report"
    shutil.copytree(SOURCE_PACK, pack_root)
    return pack_root


def loader(pack_root: Path) -> RestrictedPackLoader:
    def resolve_asset(artifact_ref: str) -> Path:
        if artifact_ref != ASSET_REF:
            raise PackLoadError("unknown trusted asset")
        return pack_root / "assets" / "neutral-industry-report-v1.docx"

    return RestrictedPackLoader(
        pack_root=pack_root,
        allowed_entrypoint_package="enterprise_wecom_digital_employee",
        asset_resolver=resolve_asset,
    )


def rewrite_yaml(path: Path, mutate) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_industry_report_pack_loads_with_frozen_profile_and_digests(tmp_path: Path) -> None:
    loaded = loader(copy_pack(tmp_path)).load()

    assert loaded.schema_version == 1
    assert loaded.pack_id == "industry-report"
    assert loaded.pack_version == "1.6.0"
    assert loaded.profile.owning_org == "战略发展部"
    assert loaded.profile.supported_playbooks == ("securities-industry-report@1",)
    assert loaded.profile.skill_refs == ("report-intake@1.1.0", "report-writing@1.2.0")
    assert loaded.profile.asset_refs == ("strategic-report-docx@1.0.0",)
    assert loaded.profile.profile_version == "1.5.0"
    assert len(loaded.profile.knowledge_refs) == 1
    knowledge = loaded.profile.knowledge_refs[0]
    assert knowledge.server == "department-knowledge"
    assert knowledge.collection == "strategic-development"
    assert knowledge.owning_org == "战略发展部"
    assert knowledge.retrieval_modes == ("keyword", "semantic", "hybrid")
    assert knowledge.default_mode == "hybrid"
    assert loaded.skill_digests == (
        "sha256:a509c2fd1bc83c1ff56dfc9e885f97a3c191b7a0f76570d265c8f0fe9c5b816e",
        "sha256:3464b3f8eec6eea0dcdfb7fab7d5fc616d8e821da3a97cb239f13f0f653da323",
    )
    assert loaded.playbooks[0].entrypoint.endswith("reports.playbook:build_playbook")
    assert loaded.playbooks[0].research_template_ref.startswith("skill://report-intake@1.1.0/")
    assert loaded.playbooks[0].research_template_path.endswith("securities-industry-internal-research.yaml")
    assert "external_factor_on_securities" in loaded.playbooks[0].allowed_research_scopes


def test_snapshot_is_content_addressed_retrievable_and_excludes_binary_asset(
    tmp_path: Path,
) -> None:
    pack_root = copy_pack(tmp_path)
    loaded = loader(pack_root).load()
    store = FilesystemPackSnapshotStore(tmp_path / "snapshots")
    snapshot = build_pack_snapshot(
        loaded,
        store=store,
        created_at=datetime(2026, 7, 13, tzinfo=UTC),
        source_repository="https://example.invalid/agentseek",
        source_commit="commit_001",
    )
    artifact_root = store.resolve(snapshot.content_artifact_id)
    shutil.rmtree(pack_root)

    assert snapshot.pack_snapshot_id.endswith(loaded.content_digest)
    assert (artifact_root / "pack.yaml").is_file()
    assert (artifact_root / "profile.yaml").is_file()
    assert (artifact_root / "skills" / "report-intake" / "SKILL.md").is_file()
    assert (artifact_root / "skills" / "report-writing" / "SKILL.md").is_file()
    assert (
        artifact_root
        / "skills"
        / "report-intake"
        / "references"
        / "securities-industry-internal-research.yaml"
    ).is_file()
    assert not tuple(artifact_root.rglob("*.docx"))
    assert store.put(loaded) == snapshot.content_artifact_id

    (artifact_root / "profile.yaml").write_text("tampered: true\n", encoding="utf-8")
    with pytest.raises(PackLoadError, match="digest does not match"):
        store.resolve(snapshot.content_artifact_id)


def test_skill_resolver_materializes_only_profile_selected_text(tmp_path: Path) -> None:
    loaded = loader(copy_pack(tmp_path)).load()
    skills_root = tmp_path / "virtual-skills"

    selected = materialize_profile_skills(loaded, skills_root)

    assert selected == (skills_root / "report-intake", skills_root / "report-writing")
    assert (skills_root / "report-intake" / "SKILL.md").is_file()
    assert (skills_root / "report-writing" / "SKILL.md").is_file()
    assert not tuple(skills_root.rglob("*.docx"))
    assert not (skills_root / "report-policy.yaml").exists()


def test_loader_rejects_tampered_skill_and_asset(tmp_path: Path) -> None:
    pack_root = copy_pack(tmp_path)
    skill = pack_root / "skills" / "report-intake" / "SKILL.md"
    skill.write_text(f"{skill.read_text(encoding='utf-8')}\ntampered\n", encoding="utf-8")
    with pytest.raises(PackLoadError, match="sha256 mismatch"):
        loader(pack_root).load()

    pack_root = copy_pack(tmp_path / "asset-case")
    asset = pack_root / "assets" / "neutral-industry-report-v1.docx"
    asset.write_bytes(b"tampered")
    with pytest.raises(PackLoadError, match="sha256 mismatch"):
        loader(pack_root).load()


def test_loader_rejects_undeclared_profile_refs_and_entrypoint_escape(tmp_path: Path) -> None:
    pack_root = copy_pack(tmp_path)
    rewrite_yaml(
        pack_root / "profile.yaml",
        lambda document: document["skill_refs"].append("undeclared@1.0.0"),
    )
    with pytest.raises(PackLoadError, match="undeclared references"):
        loader(pack_root).load()


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        (
            "skill://undeclared@1.1.0/references/securities-industry-internal-research.yaml",
            "undeclared skill version",
        ),
        (
            "skill://report-intake@1.1.0/../profile.yaml",
            "normalized and relative",
        ),
    ],
)
def test_loader_rejects_untrusted_research_template_refs(
    tmp_path: Path,
    reference: str,
    message: str,
) -> None:
    pack_root = copy_pack(tmp_path)
    rewrite_yaml(
        pack_root / "pack.yaml",
        lambda document: document["playbooks"][0].update({"research_template_ref": reference}),
    )

    with pytest.raises(PackLoadError, match=message):
        loader(pack_root).load()


def test_loader_rejects_legacy_research_template_schema(tmp_path: Path) -> None:
    pack_root = copy_pack(tmp_path)
    template = (
        pack_root
        / "skills"
        / "report-intake"
        / "references"
        / "securities-industry-internal-research.yaml"
    )
    rewrite_yaml(template, lambda document: document.update({"schema_version": 1}))

    with pytest.raises(PackLoadError, match="schema_version must be 2"):
        loader(pack_root).load()


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("provider", "filesystem", "provider must be mcp"),
        ("owning_org", "信息技术部", "owning_org must match"),
        ("contract_version", 2, "contract_version must be 1"),
        ("retrieval_modes", ["keyword", "external"], "unsupported retrieval mode"),
        ("default_mode", "semantic", "default_mode must be enabled"),
    ],
)
def test_loader_rejects_invalid_knowledge_reference(
    tmp_path: Path,
    field: str,
    value: object,
    expected: str,
) -> None:
    pack_root = copy_pack(tmp_path)

    def mutate(document: dict) -> None:
        reference = document["knowledge_refs"][0]
        reference[field] = value
        if field == "default_mode":
            reference["retrieval_modes"] = ["keyword"]

    rewrite_yaml(pack_root / "profile.yaml", mutate)

    with pytest.raises(PackLoadError, match=expected):
        loader(pack_root).load()


def test_loader_rejects_write_capability_in_knowledge_contract(tmp_path: Path) -> None:
    pack_root = copy_pack(tmp_path)

    def mutate(document: dict) -> None:
        document["knowledge_refs"][0]["tools"].append("knowledge_publish_document")

    rewrite_yaml(pack_root / "profile.yaml", mutate)

    with pytest.raises(PackLoadError, match="read-only contract"):
        loader(pack_root).load()

    pack_root = copy_pack(tmp_path / "entrypoint-case")
    rewrite_yaml(
        pack_root / "pack.yaml",
        lambda document: document["playbooks"][0].update({"entrypoint": "outside.module:run"}),
    )
    with pytest.raises(PackLoadError, match="outside the allowed package"):
        loader(pack_root).load()


def test_loader_rejects_traversal_hidden_paths_and_symlinks(tmp_path: Path) -> None:
    pack_root = copy_pack(tmp_path)
    rewrite_yaml(
        pack_root / "pack.yaml",
        lambda document: document.update({"profile": "../profile.yaml"}),
    )
    with pytest.raises(PackLoadError, match="normalized and relative"):
        loader(pack_root).load()

    pack_root = copy_pack(tmp_path / "hidden-case")
    hidden = pack_root / ".hidden.yaml"
    hidden.write_text("value: hidden\n", encoding="utf-8")
    rewrite_yaml(
        pack_root / "pack.yaml",
        lambda document: document.update({"profile": ".hidden.yaml"}),
    )
    with pytest.raises(PackLoadError, match="hidden pack path"):
        loader(pack_root).load()

    pack_root = copy_pack(tmp_path / "symlink-case")
    outside = tmp_path / "outside-skill.md"
    outside.write_text("outside", encoding="utf-8")
    skill = pack_root / "skills" / "report-intake" / "SKILL.md"
    skill.unlink()
    skill.symlink_to(outside)
    with pytest.raises(PackLoadError, match="symlink"):
        loader(pack_root).load()
