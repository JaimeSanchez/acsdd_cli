"""Tests for acsdd.capability.generator and the `capability generate` CLI
command — the missing second step of the onboarding workflow (profile
discover -> capability generate)."""

import textwrap
from pathlib import Path

import yaml
from click.testing import CliRunner

from acsdd.capability.generator import REVIEW_PLACEHOLDER, scaffold_manifest
from acsdd.capability.validator import validate_manifest
from acsdd.cli import cli

SAMPLE_PROFILE = {
    "technology_stack": {
        "language": "php",
        "version": "6.1",
        "framework": "symfony",
        "orm": "doctrine",
        "database": "postgresql",
    },
    "capability_configuration": {
        "default_adapter": "php-symfony-doctrine",
    },
    "quality_gates": {
        "automatic": ["lint:phpstan", "test:unit-passing"],
        "manual": ["architecture-review"],
    },
}


def _write_profile_yaml(path: Path, profile: dict) -> None:
    path.write_text(yaml.dump({"profile": profile}, default_flow_style=False, sort_keys=False))


def test_scaffold_manifest_is_schema_valid(tmp_path):
    manifest = scaffold_manifest(SAMPLE_PROFILE, "BE-001", "BE", name="Do The Thing")
    result = validate_manifest(tmp_path / "BE-001.yaml", manifest)
    assert result.ok, result.errors


def test_scaffold_manifest_derives_fields_from_profile():
    manifest = scaffold_manifest(SAMPLE_PROFILE, "BE-001", "BE")["capability"]
    assert manifest["id"] == "BE-001"
    assert manifest["category"] == "BE"
    assert manifest["adapters"] == [{"stack": "php-symfony-doctrine", "adapter-id": f"BE-001-{REVIEW_PLACEHOLDER}"}]
    assert set(manifest["profile_constraints"]) == {
        "language:php", "version:6.1", "framework:symfony", "orm:doctrine", "database:postgresql",
    }
    assert manifest["quality_gates"] == ["lint:phpstan", "test:unit-passing"]
    assert len(manifest["outputs"]) == 1
    assert REVIEW_PLACEHOLDER in manifest["name"]


def test_scaffold_manifest_skips_unresolved_review_required_fields():
    profile = {
        "technology_stack": {
            "language": "php",
            "framework": "[REVIEW REQUIRED — detect from manifest]",
        },
        "capability_configuration": {
            "default_adapter": "[REVIEW REQUIRED]",
        },
    }
    manifest = scaffold_manifest(profile, "BE-002", "BE")["capability"]
    assert manifest["profile_constraints"] == ["language:php"]
    assert manifest["adapters"][0]["stack"] == REVIEW_PLACEHOLDER


def test_capability_generate_auto_detects_finalized_profile(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    profiles_dir = tmp_path / "acsdd" / "profiles"
    profiles_dir.mkdir(parents=True)
    _write_profile_yaml(profiles_dir / "demo.yaml", SAMPLE_PROFILE)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "capability", "generate", "--id", "BE-001", "--category", "BE",
        "--capabilities-dir", str(tmp_path / "capabilities"),
    ])
    assert result.exit_code == 0, result.output
    assert "No --profile given, using:" in result.output
    assert str(profiles_dir / "demo.yaml") in result.output

    manifest = yaml.safe_load((tmp_path / "capabilities" / "_manifests" / "BE-001.yaml").read_text())
    assert manifest["capability"]["adapters"][0]["stack"] == "php-symfony-doctrine"


def test_capability_generate_prefers_finalized_over_draft(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    profiles_dir = tmp_path / "acsdd" / "profiles"
    profiles_dir.mkdir(parents=True)
    draft_profile = {**SAMPLE_PROFILE, "capability_configuration": {"default_adapter": "draft-adapter"}}
    _write_profile_yaml(profiles_dir / "demo-draft.yaml", draft_profile)
    _write_profile_yaml(profiles_dir / "demo.yaml", SAMPLE_PROFILE)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "capability", "generate", "--id", "BE-001", "--category", "BE",
        "--capabilities-dir", str(tmp_path / "capabilities"),
    ])
    assert result.exit_code == 0, result.output

    manifest = yaml.safe_load((tmp_path / "capabilities" / "_manifests" / "BE-001.yaml").read_text())
    assert manifest["capability"]["adapters"][0]["stack"] == "php-symfony-doctrine"


def test_capability_generate_falls_back_to_only_draft(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    profiles_dir = tmp_path / "acsdd" / "profiles"
    profiles_dir.mkdir(parents=True)
    _write_profile_yaml(profiles_dir / "demo-draft.yaml", SAMPLE_PROFILE)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "capability", "generate", "--id", "BE-001", "--category", "BE",
        "--capabilities-dir", str(tmp_path / "capabilities"),
    ])
    assert result.exit_code == 0, result.output


def test_capability_generate_errors_when_no_profiles_dir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, [
        "capability", "generate", "--id", "BE-001", "--category", "BE",
        "--capabilities-dir", str(tmp_path / "capabilities"),
    ])
    assert result.exit_code != 0
    assert "profile discover" in result.output


def test_capability_generate_errors_when_ambiguous_profiles(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    profiles_dir = tmp_path / "acsdd" / "profiles"
    profiles_dir.mkdir(parents=True)
    _write_profile_yaml(profiles_dir / "one.yaml", SAMPLE_PROFILE)
    _write_profile_yaml(profiles_dir / "two.yaml", SAMPLE_PROFILE)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "capability", "generate", "--id", "BE-001", "--category", "BE",
        "--capabilities-dir", str(tmp_path / "capabilities"),
    ])
    assert result.exit_code != 0


def test_capability_generate_creates_manifest_and_doc(tmp_path):
    profile_path = tmp_path / "demo-draft.yaml"
    _write_profile_yaml(profile_path, SAMPLE_PROFILE)
    capabilities_dir = tmp_path / "capabilities"

    runner = CliRunner()
    result = runner.invoke(cli, [
        "capability", "generate",
        "--profile", str(profile_path),
        "--id", "BE-001",
        "--category", "BE",
        "--capabilities-dir", str(capabilities_dir),
    ])
    assert result.exit_code == 0, result.output

    manifest_path = capabilities_dir / "_manifests" / "BE-001.yaml"
    doc_path = capabilities_dir / "backend" / "be-001.md"
    assert manifest_path.exists()
    assert doc_path.exists()
    assert "BE-001" in doc_path.read_text()


def test_capability_generate_refuses_overwrite_without_force(tmp_path):
    profile_path = tmp_path / "demo-draft.yaml"
    _write_profile_yaml(profile_path, SAMPLE_PROFILE)
    capabilities_dir = tmp_path / "capabilities"
    runner = CliRunner()
    argv = [
        "capability", "generate",
        "--profile", str(profile_path),
        "--id", "BE-001",
        "--category", "BE",
        "--capabilities-dir", str(capabilities_dir),
    ]

    first = runner.invoke(cli, argv)
    assert first.exit_code == 0, first.output

    second = runner.invoke(cli, argv)
    assert second.exit_code != 0
    assert "--force" in second.output

    forced = runner.invoke(cli, argv + ["--force"])
    assert forced.exit_code == 0, forced.output


def test_capability_generate_rejects_invalid_id(tmp_path):
    profile_path = tmp_path / "demo-draft.yaml"
    _write_profile_yaml(profile_path, SAMPLE_PROFILE)
    runner = CliRunner()
    result = runner.invoke(cli, [
        "capability", "generate",
        "--profile", str(profile_path),
        "--id", "not-a-valid-id",
        "--category", "BE",
        "--capabilities-dir", str(tmp_path / "capabilities"),
    ])
    assert result.exit_code != 0


def test_capability_generate_doc_stub_resolves_in_catalog_build(tmp_path):
    from acsdd.capability.loader import iter_manifests
    from acsdd.catalog.builder import build_catalog_markdown

    profile_path = tmp_path / "demo-draft.yaml"
    _write_profile_yaml(profile_path, SAMPLE_PROFILE)
    capabilities_dir = tmp_path / "capabilities"
    runner = CliRunner()
    result = runner.invoke(cli, [
        "capability", "generate",
        "--profile", str(profile_path),
        "--id", "BE-001",
        "--category", "BE",
        "--capabilities-dir", str(capabilities_dir),
    ])
    assert result.exit_code == 0, result.output

    manifests_dir = capabilities_dir / "_manifests"
    manifests = {data["capability"]["id"]: data for _, data in iter_manifests(manifests_dir)}
    md = build_catalog_markdown(manifests, docs_root=capabilities_dir, manifests_root=manifests_dir)
    assert "backend/be-001.md" in md
