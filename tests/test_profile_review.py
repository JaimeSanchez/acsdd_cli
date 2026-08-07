"""Tests for acsdd.profile.review — the [REVIEW REQUIRED] guidance engine.

Synthetic profile dicts only; nothing here scans a repository. The end-to-end
counterpart (real `profile discover` output must be fully covered by the
guidance table) lives in test_discovery.py, where the fixture repo already is.
"""

import copy
import json
import textwrap

import pytest
from click.testing import CliRunner

from acsdd.cli import cli
from acsdd.profile.generator import REVIEW_PREFIX, find_unresolved_fields
from acsdd.profile.review import (
    _MISSING,
    _get,
    _parse_path,
    guidance_for,
    review_profile,
)
from acsdd.profile.validator import get_schema

DRAFT_PROFILE = {
    "meta": {"id": "review-test", "version": "0.1.0", "status": "draft"},
    "technology_stack": {
        "language": "php",
        "version": "[REVIEW REQUIRED — detect from manifest]",
        "framework": "symfony",
        "orm": "[REVIEW REQUIRED — infer from dependencies]",
        "database": "[REVIEW REQUIRED — detect database engine]",
        "frontend": "[REVIEW REQUIRED — low confidence, verify: react]",
    },
    "engineering_standards": {
        "code_style": "[REVIEW REQUIRED]",
        "static_analysis": "phpstan",
        "test_framework": "phpunit",
        "min_coverage": 0,
    },
    "capability_configuration": {
        "default_adapter": "[REVIEW REQUIRED]",
        "frontend_adapter": None,
        "overrides": {},
    },
    "ai_execution_rules": {"max_files_per_session": 3, "max_loc_per_session": 200},
    "security_profile": {
        "cross_cutting_constraints": [
            {"id": "no-hardcoded-secrets", "enforcement": "manual",
             "tool": "[REVIEW REQUIRED]"},
            {"id": "xss-prevention", "enforcement": "manual",
             "tool": "[REVIEW REQUIRED]"},
        ],
    },
    "repository_health": {
        "current_coverage": 0,
        "tech_debt_score": "[REVIEW REQUIRED — run --depth deep]",
    },
}

CLEAN_PROFILE = {
    "meta": {"id": "clean-test", "version": "1.0.0", "status": "active"},
    "technology_stack": {
        "language": "php", "version": "6.1.11", "framework": "symfony",
        "orm": "doctrine", "database": "postgresql", "frontend": "react",
    },
    "engineering_standards": {
        "code_style": "php-cs-fixer", "static_analysis": "phpstan",
        "test_framework": "phpunit", "min_coverage": 80,
    },
    "capability_configuration": {
        "default_adapter": "php-symfony-doctrine", "frontend_adapter": "react",
        "overrides": {"DB-001": {"adapter": "raw-sql"}},
    },
    "ai_execution_rules": {"max_files_per_session": 5, "max_loc_per_session": 200},
    "security_profile": {
        "cross_cutting_constraints": [
            {"id": "no-hardcoded-secrets", "enforcement": "automatic", "tool": "gitleaks"},
        ],
    },
    "repository_health": {"current_coverage": 80, "tech_debt_score": "low"},
}


def test_every_unresolved_field_gets_guidance():
    report = review_profile(DRAFT_PROFILE)
    assert report.profile_id == "review-test"
    assert len(report.unresolved) == len(find_unresolved_fields(DRAFT_PROFILE))
    assert report.unresolved, "fixture should have placeholders"
    for entry in report.unresolved:
        assert entry.guidance.has_guidance is True, entry.path
        assert entry.guidance.what
        assert entry.guidance.detection_attempted
        assert entry.placeholder.startswith(REVIEW_PREFIX)


def test_constraint_guidance_keys_off_id_not_index():
    # Same two constraints, opposite order. Guidance must follow the `id`, since
    # the list index is an artifact of ProfileGenerator's literal ordering and a
    # human is free to reorder the list.
    reordered = copy.deepcopy(DRAFT_PROFILE)
    reordered["security_profile"]["cross_cutting_constraints"].reverse()

    original = {e.path: e for e in review_profile(DRAFT_PROFILE).unresolved}
    swapped = {e.path: e for e in review_profile(reordered).unresolved}

    path0 = "security_profile.cross_cutting_constraints[0].tool"
    path1 = "security_profile.cross_cutting_constraints[1].tool"

    assert original[path0].discriminator == "no-hardcoded-secrets"
    assert swapped[path0].discriminator == "xss-prevention"
    assert original[path0].guidance.key == swapped[path1].guidance.key
    assert "gitleaks" in original[path0].guidance.examples
    assert "gitleaks" not in swapped[path0].guidance.examples


def test_constraint_enforcement_link_is_reindexed_to_the_concrete_path():
    entry = next(e for e in review_profile(DRAFT_PROFILE).unresolved
                 if e.path == "security_profile.cross_cutting_constraints[0].tool")
    links = entry.to_dict()["linked_fields"]
    assert links, "no-hardcoded-secrets should link its sibling enforcement field"
    assert links[0]["path"] == "security_profile.cross_cutting_constraints[0].enforcement"


def test_unknown_path_falls_back_without_raising():
    profile = copy.deepcopy(DRAFT_PROFILE)
    profile["technology_stack"]["made_up_field"] = "[REVIEW REQUIRED — invented]"

    entry = next(e for e in review_profile(profile).unresolved
                 if e.path == "technology_stack.made_up_field")
    assert entry.guidance.has_guidance is False
    assert entry.guidance.resolution == "edit"
    # The raw placeholder is echoed back — it usually says what was attempted.
    assert entry.guidance.detection_attempted == "[REVIEW REQUIRED — invented]"


def test_unknown_constraint_id_falls_back_to_the_family_default():
    profile = copy.deepcopy(DRAFT_PROFILE)
    profile["security_profile"]["cross_cutting_constraints"][0]["id"] = "made-up-constraint"

    entry = next(e for e in review_profile(profile).unresolved
                 if e.path == "security_profile.cross_cutting_constraints[0].tool")
    # Still registered guidance (the family default), just not a member entry.
    assert entry.guidance.has_guidance is True
    assert entry.guidance.key == "security_profile.cross_cutting_constraints[].tool"


def test_clean_profile_yields_no_unresolved_fields_or_advisories():
    report = review_profile(CLEAN_PROFILE)
    assert report.unresolved == []
    assert report.advisories == []


def test_tech_debt_score_is_a_rerun_not_an_edit():
    entry = next(e for e in review_profile(DRAFT_PROFILE).unresolved
                 if e.path == "repository_health.tech_debt_score")
    assert entry.guidance.resolution == "rerun-discovery"
    assert "--depth deep" in entry.guidance.action
    assert entry.guidance.allowed_values == ["low", "medium", "high"]


def test_advisories_are_self_extinguishing():
    report = review_profile(DRAFT_PROFILE)
    paths = {a.path for a in report.advisories}
    assert paths == {
        "engineering_standards.min_coverage",
        "ai_execution_rules.max_files_per_session",
        "capability_configuration.overrides",
    }

    touched = copy.deepcopy(DRAFT_PROFILE)
    touched["engineering_standards"]["min_coverage"] = 80
    assert "engineering_standards.min_coverage" not in {
        a.path for a in review_profile(touched).advisories
    }


def test_advisories_never_count_as_unresolved():
    report = review_profile(DRAFT_PROFILE)
    advisory_paths = {a.path for a in report.advisories}
    unresolved_paths = {u.path for u in report.unresolved}
    assert advisory_paths.isdisjoint(unresolved_paths)
    assert report.to_dict()["unresolved_count"] == len(report.unresolved)


def test_allowed_values_match_the_json_schema():
    # The module keeps these as plain constants for readability; this is the
    # guard that stops them drifting from schemas/profile.schema.json.
    schema = get_schema()["properties"]["profile"]["properties"]

    constraint_props = (schema["security_profile"]["properties"]
                        ["cross_cutting_constraints"]["items"]["properties"])
    entry = next(e for e in review_profile(DRAFT_PROFILE).unresolved
                 if e.path == "security_profile.cross_cutting_constraints[0].tool")
    link = entry.to_dict()["linked_fields"][0]
    assert link["allowed_values"] == constraint_props["enforcement"]["enum"]

    tech_debt = schema["repository_health"]["properties"]["tech_debt_score"]
    schema_enum = next(branch["enum"] for branch in tech_debt["oneOf"] if "enum" in branch)
    guidance = guidance_for("repository_health.tech_debt_score", DRAFT_PROFILE)
    assert guidance.allowed_values == schema_enum


def test_evidence_hints_are_annotated_against_the_repo(tmp_path):
    (tmp_path / "composer.lock").write_text("{}")

    entry = next(e for e in review_profile(DRAFT_PROFILE, repo_path=tmp_path).unresolved
                 if e.path == "technology_stack.version")
    by_hint = {e.hint: e.found for e in entry.evidence}
    assert by_hint["composer.lock"] is True
    assert by_hint["Dockerfile (FROM tag)"] is False
    # Prose hints stay unannotated rather than claiming a confident False.
    assert by_hint["pom.xml / build.gradle"] is None


def test_evidence_is_unannotated_without_a_repo_path():
    entry = next(e for e in review_profile(DRAFT_PROFILE).unresolved
                 if e.path == "technology_stack.version")
    assert all(hint.found is None for hint in entry.evidence)


@pytest.mark.parametrize("path", find_unresolved_fields(DRAFT_PROFILE))
def test_parse_path_roundtrips_find_unresolved_fields_output(path):
    # The coupling invariant between review.py and generator.py: every path
    # find_unresolved_fields emits must resolve back to the placeholder it was
    # emitted for. If either walk changes shape, this fails first.
    value = _get(DRAFT_PROFILE, _parse_path(path))
    assert value is not _MISSING
    assert isinstance(value, str) and value.startswith(REVIEW_PREFIX)


def test_get_returns_missing_rather_than_raising():
    assert _get(DRAFT_PROFILE, _parse_path("nope.not.here")) is _MISSING
    assert _get(DRAFT_PROFILE, _parse_path("technology_stack[0]")) is _MISSING
    assert _get(DRAFT_PROFILE, _parse_path(
        "security_profile.cross_cutting_constraints[99].tool")) is _MISSING


# ---------------------------------------------------------------------
# acsdd profile review
# ---------------------------------------------------------------------

DRAFT_YAML = textwrap.dedent("""
    profile:
      meta:
        id: "cli-test"
        version: "0.1.0"
        status: "draft"
      technology_stack:
        language: "php"
        framework: "symfony"
        database: "[REVIEW REQUIRED — detect database engine]"
      engineering_standards:
        code_style: "[REVIEW REQUIRED]"
        min_coverage: 0
      ai_execution_rules:
        max_files_per_session: 3
""")

CLEAN_YAML = DRAFT_YAML.replace(
    '"[REVIEW REQUIRED — detect database engine]"', '"postgresql"'
).replace('code_style: "[REVIEW REQUIRED]"', 'code_style: "php-cs-fixer"')


def _write_draft(tmp_path, body=DRAFT_YAML, name="cli-test-draft.yaml"):
    profiles_dir = tmp_path / "acsdd" / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    path = profiles_dir / name
    path.write_text(body, encoding="utf-8")
    return path


def test_review_lists_unresolved_fields_and_exits_zero(tmp_path):
    # Deliberately NOT a gate: unresolved fields are this command's expected
    # input, so finding them is a success. `validate --strict` is the gate.
    draft = _write_draft(tmp_path)
    result = CliRunner().invoke(cli, ["profile", "review", str(draft)])

    assert result.exit_code == 0, result.output
    assert "2 unresolved [REVIEW REQUIRED] field(s):" in result.output
    assert "technology_stack.database" in result.output
    assert "resolve:" in result.output
    assert "Also worth setting" in result.output


def test_review_json_is_the_only_thing_on_stdout(tmp_path, monkeypatch):
    _write_draft(tmp_path)
    monkeypatch.chdir(tmp_path)
    # No PROFILE_PATH, so the "using: ..." line fires — it must go to stderr,
    # or the skill consuming this payload can't parse it.
    result = CliRunner().invoke(cli, ["profile", "review", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["profile_id"] == "cli-test"
    assert payload["unresolved_count"] == 2
    assert payload["schema_errors"] == []
    assert {u["path"] for u in payload["unresolved"]} == {
        "technology_stack.database", "engineering_standards.code_style",
    }
    assert "No PROFILE_PATH given" in result.stderr


def test_review_auto_detects_the_draft_over_a_finalized_profile(tmp_path, monkeypatch):
    _write_draft(tmp_path)
    _write_draft(tmp_path, body=CLEAN_YAML, name="cli-test.yaml")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["profile", "review"])
    assert result.exit_code == 0, result.output
    assert "cli-test-draft.yaml" in result.output
    assert "2 unresolved" in result.output


def test_review_reports_pass_on_a_resolved_profile(tmp_path):
    path = _write_draft(tmp_path, body=CLEAN_YAML, name="cli-test.yaml")
    result = CliRunner().invoke(cli, ["profile", "review", str(path)])

    assert result.exit_code == 0, result.output
    assert "no unresolved [REVIEW REQUIRED] fields" in result.output
    assert "acsdd profile create" in result.output


def test_review_warns_but_continues_on_a_schema_invalid_draft(tmp_path):
    # A draft that's both broken and incomplete is exactly the one that most
    # needs hints, so schema failures warn rather than gate.
    broken = DRAFT_YAML.replace('version: "0.1.0"', 'version: "not-a-version"')
    draft = _write_draft(tmp_path, body=broken)

    result = CliRunner().invoke(cli, ["profile", "review", str(draft)])
    assert result.exit_code == 0, result.output
    assert "WARN" in result.output
    assert "technology_stack.database" in result.output


def test_review_json_carries_schema_errors_instead_of_warning(tmp_path):
    broken = DRAFT_YAML.replace('version: "0.1.0"', 'version: "not-a-version"')
    draft = _write_draft(tmp_path, body=broken)

    result = CliRunner().invoke(cli, ["profile", "review", str(draft), "--json"])
    payload = json.loads(result.stdout)
    assert payload["schema_errors"]
    assert payload["unresolved_count"] == 2


def test_review_errors_when_no_profile_can_be_found(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["profile", "review"])

    assert result.exit_code == 1
    assert "ERROR: no PROFILE_PATH given" in result.output


def test_review_annotates_evidence_against_repo_path(tmp_path):
    draft = _write_draft(tmp_path)
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")

    result = CliRunner().invoke(cli, [
        "profile", "review", str(draft), "--json", "--repo-path", str(tmp_path),
    ])
    payload = json.loads(result.stdout)
    entry = next(u for u in payload["unresolved"]
                 if u["path"] == "technology_stack.database")
    by_hint = {e["hint"]: e["found"] for e in entry["evidence"]}
    assert by_hint["docker-compose.yml"] is True
    assert by_hint[".env.example"] is False
