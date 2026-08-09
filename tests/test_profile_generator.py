"""Tests for acsdd.profile.generator and the `profile create` CLI command —
the gate that promotes a reviewed draft profile into an active one."""

import textwrap
from datetime import date
from pathlib import Path

from click.testing import CliRunner

from acsdd.cli import cli
from acsdd.profile.generator import find_unresolved_fields, finalize_profile

DRAFT_PROFILE = {
    "meta": {"id": "test-profile", "version": "0.1.0", "status": "draft"},
    "technology_stack": {
        "language": "php",
        "framework": "symfony",
        "database": "[REVIEW REQUIRED — detect database engine]",
    },
    "engineering_standards": {"code_style": "phpstan"},
    "ai_execution_rules": {"max_files_per_session": 3},
    "security_profile": {
        "cross_cutting_constraints": [
            {"id": "no-hardcoded-secrets", "tool": "[REVIEW REQUIRED]"},
            {"id": "xss-prevention", "tool": "eslint-security"},
        ],
    },
}

CLEAN_PROFILE = {
    "meta": {"id": "test-profile", "version": "0.1.0", "status": "draft"},
    "technology_stack": {"language": "php", "framework": "symfony", "database": "postgresql"},
    "engineering_standards": {"code_style": "phpstan"},
    "ai_execution_rules": {"max_files_per_session": 3},
    "security_profile": {
        "cross_cutting_constraints": [
            {"id": "no-hardcoded-secrets", "tool": "gitleaks"},
            {"id": "xss-prevention", "tool": "eslint-security"},
        ],
    },
}

VALID_DRAFT_YAML = textwrap.dedent("""
    profile:
      meta:
        id: "test-profile"
        version: "0.1.0"
        status: "draft"
      technology_stack:
        language: "php"
        framework: "symfony"
        database: "[REVIEW REQUIRED — detect database engine]"
      engineering_standards:
        code_style: "phpstan"
      ai_execution_rules:
        max_files_per_session: 3
""")

VALID_CLEAN_DRAFT_YAML = textwrap.dedent("""
    profile:
      meta:
        id: "test-profile"
        version: "0.1.0"
        status: "draft"
      technology_stack:
        language: "php"
        framework: "symfony"
        database: "postgresql"
      engineering_standards:
        code_style: "phpstan"
      ai_execution_rules:
        max_files_per_session: 3
""")


def test_find_unresolved_fields_locates_nested_dict_and_list_entries():
    unresolved = find_unresolved_fields(DRAFT_PROFILE)
    assert "technology_stack.database" in unresolved
    assert "security_profile.cross_cutting_constraints[0].tool" in unresolved
    assert "security_profile.cross_cutting_constraints[1].tool" not in unresolved
    assert len(unresolved) == 2


def test_find_unresolved_fields_empty_for_clean_profile():
    assert find_unresolved_fields(CLEAN_PROFILE) == []


def test_finalize_profile_bumps_default_version_and_status():
    finalized = finalize_profile(CLEAN_PROFILE)
    assert finalized["meta"]["status"] == "active"
    assert finalized["meta"]["version"] == "1.0.0"
    assert finalized["meta"]["last_updated"] == date.today().isoformat()
    # original untouched
    assert CLEAN_PROFILE["meta"]["version"] == "0.1.0"


def test_finalize_profile_leaves_custom_version_untouched():
    profile = {**CLEAN_PROFILE, "meta": {**CLEAN_PROFILE["meta"], "version": "2.3.1"}}
    finalized = finalize_profile(profile)
    assert finalized["meta"]["version"] == "2.3.1"
    assert finalized["meta"]["status"] == "active"


def test_profile_create_refuses_with_unresolved_fields(tmp_path):
    draft_path = tmp_path / "test-profile-draft.yaml"
    draft_path.write_text(VALID_DRAFT_YAML)

    runner = CliRunner()
    result = runner.invoke(cli, ["profile", "create", "--draft", str(draft_path)])
    assert result.exit_code != 0
    assert "technology_stack.database" in result.output


def test_profile_create_succeeds_and_writes_active_profile(tmp_path):
    draft_path = tmp_path / "test-profile-draft.yaml"
    draft_path.write_text(VALID_CLEAN_DRAFT_YAML)

    runner = CliRunner()
    result = runner.invoke(cli, ["profile", "create", "--draft", str(draft_path)])
    assert result.exit_code == 0, result.output

    output_path = tmp_path / "test-profile.yaml"
    assert output_path.exists()

    validate_result = runner.invoke(cli, ["profile", "validate", str(output_path)])
    assert validate_result.exit_code == 0, validate_result.output

    import yaml
    data = yaml.safe_load(output_path.read_text())
    assert data["profile"]["meta"]["status"] == "active"
    assert data["profile"]["meta"]["version"] == "1.0.0"


def test_profile_create_refuses_overwrite_without_force(tmp_path):
    draft_path = tmp_path / "test-profile-draft.yaml"
    draft_path.write_text(VALID_CLEAN_DRAFT_YAML)
    runner = CliRunner()

    first = runner.invoke(cli, ["profile", "create", "--draft", str(draft_path)])
    assert first.exit_code == 0, first.output

    second = runner.invoke(cli, ["profile", "create", "--draft", str(draft_path)])
    assert second.exit_code != 0
    assert "--force" in second.output

    forced = runner.invoke(cli, ["profile", "create", "--draft", str(draft_path), "--force"])
    assert forced.exit_code == 0, forced.output


def test_profile_create_auto_detects_single_draft(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    profiles_dir = tmp_path / ".acsdd" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "test-profile-draft.yaml").write_text(VALID_CLEAN_DRAFT_YAML)

    runner = CliRunner()
    result = runner.invoke(cli, ["profile", "create"])
    assert result.exit_code == 0, result.output
    assert "No --draft given, using:" in result.output
    assert (profiles_dir / "test-profile.yaml").exists()


def test_profile_create_errors_when_no_draft_found(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["profile", "create"])
    assert result.exit_code != 0
    assert "profile discover" in result.output


def test_profile_create_ignores_finalized_profile_when_auto_detecting_draft(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    profiles_dir = tmp_path / ".acsdd" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "test-profile-draft.yaml").write_text(VALID_CLEAN_DRAFT_YAML)
    (profiles_dir / "test-profile.yaml").write_text(VALID_CLEAN_DRAFT_YAML.replace('"draft"', '"active"'))

    runner = CliRunner()
    result = runner.invoke(cli, ["profile", "create", "--force"])
    assert result.exit_code == 0, result.output
    assert str(profiles_dir / "test-profile-draft.yaml") in result.output


def test_profile_validate_auto_detects_profile(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    profiles_dir = tmp_path / ".acsdd" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "test-profile-draft.yaml").write_text(VALID_DRAFT_YAML)

    runner = CliRunner()
    result = runner.invoke(cli, ["profile", "validate"])
    assert result.exit_code == 0, result.output
    assert "No PROFILE_PATH given, using:" in result.output


def test_profile_validate_errors_when_no_profile_found(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["profile", "validate"])
    assert result.exit_code != 0
    assert "profile discover" in result.output


def test_profile_create_refuses_invalid_schema(tmp_path):
    draft_path = tmp_path / "bad-draft.yaml"
    draft_path.write_text("profile:\n  meta:\n    id: bad\n")
    runner = CliRunner()
    result = runner.invoke(cli, ["profile", "create", "--draft", str(draft_path)])
    assert result.exit_code != 0


# ---------------------------------------------------------------------
# profile remove
# ---------------------------------------------------------------------

def _write_all_four_artifacts(profiles_dir: Path, profile_id: str = "demo") -> list:
    """Everything discover + create between them leave behind."""
    profiles_dir.mkdir(parents=True, exist_ok=True)
    paths = [
        profiles_dir / f"{profile_id}-draft.yaml",
        profiles_dir / f"{profile_id}-discovery-report.md",
        profiles_dir / f"{profile_id}-recommendations.md",
        profiles_dir / f"{profile_id}.yaml",
    ]
    for path in paths:
        path.write_text("placeholder\n")
    return paths


def test_profile_remove_deletes_every_artifact(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    paths = _write_all_four_artifacts(tmp_path / ".acsdd" / "profiles")

    runner = CliRunner()
    result = runner.invoke(cli, ["profile", "remove", "demo", "--force"])
    assert result.exit_code == 0, result.output
    assert not any(p.exists() for p in paths)
    assert result.output.count("Removed") == 4


def test_profile_remove_ignores_a_partial_artifact_set(tmp_path):
    profiles_dir = tmp_path / ".acsdd" / "profiles"
    profiles_dir.mkdir(parents=True)
    draft = profiles_dir / "demo-draft.yaml"
    draft.write_text("placeholder\n")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "profile", "remove", "demo", "--profiles-dir", str(profiles_dir), "--force",
    ])
    assert result.exit_code == 0, result.output
    assert not draft.exists()


def test_profile_remove_leaves_other_profiles_alone(tmp_path):
    profiles_dir = tmp_path / ".acsdd" / "profiles"
    _write_all_four_artifacts(profiles_dir, "demo")
    keepers = _write_all_four_artifacts(profiles_dir, "other")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "profile", "remove", "demo", "--profiles-dir", str(profiles_dir), "--force",
    ])
    assert result.exit_code == 0, result.output
    assert all(p.exists() for p in keepers)


def test_profile_remove_refuses_without_force(tmp_path):
    profiles_dir = tmp_path / ".acsdd" / "profiles"
    paths = _write_all_four_artifacts(profiles_dir)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "profile", "remove", "demo", "--profiles-dir", str(profiles_dir),
    ])
    assert result.exit_code == 1
    assert "Would remove:" in result.output
    assert all(p.exists() for p in paths)


def test_profile_remove_keeps_the_profiles_dir_when_the_last_one_goes(tmp_path):
    # An empty .acsdd/profiles still means "onboarded"; a missing one doesn't.
    profiles_dir = tmp_path / ".acsdd" / "profiles"
    _write_all_four_artifacts(profiles_dir)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "profile", "remove", "demo", "--profiles-dir", str(profiles_dir), "--force",
    ])
    assert result.exit_code == 0, result.output
    assert profiles_dir.is_dir()


def test_profile_remove_unknown_id_exits_1(tmp_path):
    profiles_dir = tmp_path / ".acsdd" / "profiles"
    profiles_dir.mkdir(parents=True)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "profile", "remove", "nope", "--profiles-dir", str(profiles_dir), "--force",
    ])
    assert result.exit_code == 1
    assert "nope" in result.output


def test_profile_remove_errors_when_repo_has_no_profiles_dir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["profile", "remove", "demo", "--force"])
    assert result.exit_code == 1
    assert ".acsdd/profiles" in result.output
