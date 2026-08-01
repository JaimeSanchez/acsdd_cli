"""Tests for the `cli` group's bare-invocation behavior (no subcommand) —
the first-run welcome banner shown in a repo that hasn't been onboarded
through the Quickstart sequence yet."""

from pathlib import Path

import yaml
from click.testing import CliRunner

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


def test_bare_invocation_shows_welcome_when_not_onboarded(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, [])

    assert result.exit_code == 0, result.output
    assert "AI-Collaborative Software Development & Delivery" in result.output
    assert "acsdd profile discover ." in result.output
    assert "[ ] 1." in result.output


def test_bare_invocation_shows_help_when_onboarded(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    profiles_dir = tmp_path / "acsdd" / "profiles"
    profiles_dir.mkdir(parents=True)
    _write_profile_yaml(profiles_dir / "demo.yaml", SAMPLE_PROFILE)

    manifests_dir = tmp_path / "capabilities" / "_manifests"
    manifests_dir.mkdir(parents=True)
    (manifests_dir / "BE-001.yaml").write_text("capability:\n  id: BE-001\n")

    runner = CliRunner()
    result = runner.invoke(cli, [])

    assert result.exit_code == 0, result.output
    assert "█████╗" not in result.output
    assert "Usage:" in result.output


def test_bare_invocation_partial_onboarding_tracks_progress(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    profiles_dir = tmp_path / "acsdd" / "profiles"
    profiles_dir.mkdir(parents=True)
    _write_profile_yaml(profiles_dir / "demo-draft.yaml", SAMPLE_PROFILE)

    runner = CliRunner()
    result = runner.invoke(cli, [])

    assert result.exit_code == 0, result.output
    assert "AI-Collaborative Software Development & Delivery" in result.output
    assert "[x] 1." in result.output
    assert "[ ] 2." in result.output


def test_subcommand_output_unaffected_by_welcome_banner(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["capability", "list", "--manifests-dir", str(tmp_path / "_manifests")])

    assert "█████╗" not in result.output
