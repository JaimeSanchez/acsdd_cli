"""Tests for the ACSDD CLI's profile validator."""

import textwrap
from pathlib import Path

from acsdd.profile.validator import validate_profile_file

VALID_PROFILE = textwrap.dedent("""
    profile:
      meta:
        id: "test-profile"
        version: "0.1.0"
      technology_stack:
        language: "php"
        framework: "symfony"
      engineering_standards:
        code_style: "phpstan"
      ai_execution_rules:
        max_files_per_session: 3
""")


def test_valid_profile_passes(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text(VALID_PROFILE)
    result = validate_profile_file(p)
    assert result.ok, result.errors


def test_profile_missing_required_section_fails(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text(VALID_PROFILE.replace("engineering_standards", "engineering_standardz"))
    result = validate_profile_file(p)
    assert not result.ok


def test_profile_bad_version_pattern_fails(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text(VALID_PROFILE.replace('"0.1.0"', '"0.1.0-draft"'))
    result = validate_profile_file(p)
    assert not result.ok
    assert any("version" in e for e in result.errors)


def test_profile_bad_yaml_fails(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text("profile: [unterminated")
    result = validate_profile_file(p)
    assert not result.ok
