"""Tests for acsdd.capability.recommender and the `capability recommend` CLI
command — the step that answers *which* capabilities a repo should have, which
`capability generate` presumes you already know.

test_templates_stay_consistent_with_the_schemas is a guard rather than
coverage, in the same family as test_discovery.py's placeholder-drift test and
test_profile_review.py's enum check. It fails when the rule table drifts away
from the profile that real discovery produces or the categories the capability
schema allows. Don't weaken it to make a change pass — fix the table.
"""

import copy
import json
import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from acsdd.capability.loader import iter_manifests
from acsdd.capability.recommender import (
    _TEMPLATES,
    find_stale,
    recommend,
)
from acsdd.cli import _CAP_ID_PATTERN, cli

# A profile matching the repo's own example manifests: Symfony 4.4 / Doctrine 2.
MATCHING_PROFILE = {
    "meta": {"id": "example", "version": "1.0.0", "status": "active"},
    "technology_stack": {
        "language": "php",
        "version": "4.4 (symfony/framework-bundle: ~4.4)",
        "framework": "symfony",
        "orm": "doctrine",
        "database": "mysql",
        "additional": [
            {"name": "doctrine/orm", "version": "2.7.5"},
            {"name": "doctrine/dbal", "version": "2.10.2"},
        ],
    },
    "engineering_standards": {
        "code_style": "php-cs-fixer",
        "static_analysis": "phpstan",
        "test_framework": "phpunit",
        "min_coverage": 60,
    },
    "capability_configuration": {
        "default_adapter": "php-symfony-doctrine",
        "frontend_adapter": None,
    },
    "quality_gates": {
        "automatic": ["static-analysis:phpstan", "test:unit-passing"],
        "manual": ["architecture-review"],
    },
    "security_profile": {
        "cross_cutting_constraints": [
            {"id": "sql-injection-prevention", "name": "SQL injection prevention",
             "enforcement": "manual", "tool": "[REVIEW REQUIRED]",
             "rule": "No raw SQL string concatenation with user input."},
        ],
    },
}


@pytest.fixture
def real_manifests():
    manifests_dir = Path(__file__).parent.parent / ".acsdd" / "capabilities" / "_manifests"
    return list(iter_manifests(manifests_dir))


def _slugs(report, status=None):
    return {r.slug for r in report.recommended if status is None or r.status == status}


# ---------------------------------------------------------------------
# Trait gating
# ---------------------------------------------------------------------

def test_orm_trait_yields_the_database_capability_set():
    report = recommend(MATCHING_PROFILE, [])
    assert {"db.entity-inventory", "db.entity-create", "db.entity-update",
            "db.migration"} <= _slugs(report)


def test_no_orm_means_no_database_capabilities():
    profile = copy.deepcopy(MATCHING_PROFILE)
    del profile["technology_stack"]["orm"]

    report = recommend(profile, [])
    assert not [r for r in report.recommended if r.category == "DB"]
    # The rest of the table is unaffected — one absent trait is not a reason to
    # stop recommending anything else.
    assert "be.endpoint-create" in _slugs(report)


def test_a_placeholder_is_not_a_detected_trait():
    """Recommending the Doctrine set off "[REVIEW REQUIRED — infer from
    dependencies]" would be recommending it off nothing."""
    profile = copy.deepcopy(MATCHING_PROFILE)
    profile["technology_stack"]["orm"] = "[REVIEW REQUIRED — infer from dependencies]"

    assert not [r for r in recommend(profile, []).recommended if r.category == "DB"]


def test_frontend_trait_yields_frontend_capabilities():
    profile = copy.deepcopy(MATCHING_PROFILE)
    profile["technology_stack"]["frontend"] = "react"

    report = recommend(profile, [])
    assert {"fe.component-create", "fe.api-client", "fe.state-slice"} <= _slugs(report)
    assert "Create React Component" in {r.name for r in report.recommended}


def test_names_are_formatted_from_the_profiles_own_stack():
    names = {r.name for r in recommend(MATCHING_PROFILE, []).recommended}
    assert "Identify Doctrine Entities" in names
    assert "Create Symfony Endpoint" in names


# ---------------------------------------------------------------------
# blocked_by: recommended anyway, with the gap stated
# ---------------------------------------------------------------------

def test_a_repo_with_no_test_framework_still_gets_the_test_capability():
    profile = copy.deepcopy(MATCHING_PROFILE)
    profile["engineering_standards"]["test_framework"] = "[REVIEW REQUIRED]"

    report = recommend(profile, [])
    by_slug = {r.slug: r for r in report.recommended}

    # "you have no tests" is the finding, not a reason to say nothing.
    assert "test.unit-authoring" in by_slug
    assert by_slug["test.unit-authoring"].blocked_by
    # ...but a capability that exists only to raise coverage genuinely needs a
    # runner, so it drops out entirely.
    assert "test.coverage-gate" not in by_slug


def test_a_constraint_with_no_tool_is_recommended_and_flagged():
    report = recommend(MATCHING_PROFILE, [])
    sec = {r.slug: r for r in report.recommended if r.category == "SEC"}

    assert "sec.sql-injection-prevention" in sec
    assert sec["sec.sql-injection-prevention"].blocked_by


def test_a_not_applicable_constraint_is_skipped():
    profile = copy.deepcopy(MATCHING_PROFILE)
    profile["security_profile"]["cross_cutting_constraints"][0]["enforcement"] = "not-applicable"

    assert not [r for r in recommend(profile, []).recommended if r.category == "SEC"]


# ---------------------------------------------------------------------
# Coverage and id allocation
# ---------------------------------------------------------------------

def test_existing_manifests_cover_the_database_templates(real_manifests):
    report = recommend(MATCHING_PROFILE, real_manifests)
    db = {r.slug: r for r in report.recommended if r.category == "DB"}

    assert all(r.status == "covered" for r in db.values()), {
        s: r.status for s, r in db.items()}
    assert db["db.entity-inventory"].covered_by == ["DB-001"]
    # Nothing to create, so nothing to number.
    assert db["db.entity-inventory"].suggested_id is None
    assert db["db.entity-inventory"].generate_command is None


def test_existing_by_category_reports_what_is_actually_there(real_manifests):
    report = recommend(MATCHING_PROFILE, real_manifests)
    assert report.existing_by_category["DB"] == [
        "DB-001: Identify Doctrine Entities",
        "DB-002: Create Doctrine Entity",
        "DB-003: Update Doctrine Entity",
        "DB-004: Create Doctrine Migration",
    ]


def test_suggested_ids_skip_taken_numbers_and_never_collide(real_manifests):
    report = recommend(MATCHING_PROFILE, real_manifests)
    proposed = [r.suggested_id for r in report.missing]

    assert len(proposed) == len(set(proposed))
    assert all(re.match(_CAP_ID_PATTERN, cap_id) for cap_id in proposed)
    # DB-001..004 are taken; a fifth DB template would have to start at DB-005.
    assert not {"DB-001", "DB-002", "DB-003", "DB-004"} & set(proposed)
    assert [r.suggested_id for r in report.missing if r.category == "BE"] == [
        "BE-001", "BE-002", "BE-003"]


# ---------------------------------------------------------------------
# Staleness — the half that only shows up on the second run
# ---------------------------------------------------------------------

def test_a_profile_matching_its_manifests_reports_no_drift(real_manifests):
    """The no-false-positives half. A drift report that cries wolf on a repo
    that hasn't changed is one nobody reads."""
    stale, _advisories = find_stale(MATCHING_PROFILE, real_manifests)
    assert stale == []


def test_a_major_library_upgrade_is_drift(real_manifests):
    profile = copy.deepcopy(MATCHING_PROFILE)
    profile["technology_stack"]["additional"] = [
        {"name": "doctrine/orm", "version": "3.6.1"},
        {"name": "doctrine/dbal", "version": "2.10.2"},
    ]

    stale, _ = find_stale(profile, real_manifests)
    orm = [s for s in stale if s.manifest_value.startswith("doctrine/orm")]

    assert {s.id for s in orm} == {"DB-001", "DB-002", "DB-003", "DB-004"}
    assert orm[0].manifest_value == "doctrine/orm:^2.6"
    assert orm[0].profile_value == "doctrine/orm:3.6.1"
    assert orm[0].field == "profile_constraints"
    # dbal didn't move, so it must not appear.
    assert not [s for s in stale if s.manifest_value.startswith("doctrine/dbal")]


def test_a_framework_upgrade_reaches_its_component_packages(real_manifests):
    """technology_stack.version is the framework's version; the manifests pin
    symfony/framework-bundle. Nothing maps one to the other except the
    framework-name heuristic, so this is what proves it fires."""
    profile = copy.deepcopy(MATCHING_PROFILE)
    profile["technology_stack"]["version"] = "6.1 (symfony/framework-bundle: 6.1.*)"

    stale, _ = find_stale(profile, real_manifests)
    hits = [s for s in stale if s.manifest_value.startswith("symfony/framework-bundle")]

    assert hits, [s.manifest_value for s in stale]
    # The compound version field is not echoed back raw into the finding.
    assert hits[0].profile_value == "symfony/framework-bundle:6.1"


def test_minor_version_movement_is_not_drift(real_manifests):
    profile = copy.deepcopy(MATCHING_PROFILE)
    profile["technology_stack"]["additional"] = [
        {"name": "doctrine/orm", "version": "2.20.4"},
    ]

    stale, _ = find_stale(profile, real_manifests)
    assert not [s for s in stale if s.manifest_value.startswith("doctrine/orm")]


def test_constraint_keys_the_profile_cannot_speak_to_are_ignored():
    manifest = (Path("XX-001.yaml"), {"capability": {
        "id": "XX-001", "category": "BE", "name": "Thing",
        "profile_constraints": ["some/unknown-package:^1.0", "orm:doctrine"],
        "adapters": [{"stack": "php-symfony-doctrine", "adapter-id": "XX-001-A"}],
        "quality_gates": [],
    }})

    stale, _ = find_stale(MATCHING_PROFILE, [manifest])
    assert stale == []


def test_a_field_name_constraint_that_disagrees_is_drift():
    """capability.generator writes field-name keys (orm:doctrine); the shipped
    example manifests write package names. Both conventions have to work."""
    manifest = (Path("XX-001.yaml"), {"capability": {
        "id": "XX-001", "category": "BE", "name": "Thing",
        "profile_constraints": ["orm:eloquent", "framework:symfony"],
        "adapters": [{"stack": "php-symfony-doctrine", "adapter-id": "XX-001-A"}],
        "quality_gates": [],
    }})

    stale, _ = find_stale(MATCHING_PROFILE, [manifest])
    assert [s.manifest_value for s in stale] == ["orm:eloquent"]


def test_an_adapter_the_profile_no_longer_targets_is_drift(real_manifests):
    profile = copy.deepcopy(MATCHING_PROFILE)
    profile["capability_configuration"]["default_adapter"] = "php-laravel-eloquent"

    stale, _ = find_stale(profile, real_manifests)
    adapters = [s for s in stale if s.field == "adapters[].stack"]

    assert {s.id for s in adapters} == {"DB-001", "DB-002", "DB-003", "DB-004"}
    assert adapters[0].profile_value == "php-laravel-eloquent"


def test_a_gate_naming_a_replaced_tool_is_drift(real_manifests):
    profile = copy.deepcopy(MATCHING_PROFILE)
    profile["quality_gates"]["automatic"] = ["static-analysis:psalm", "test:unit-passing"]

    stale, _ = find_stale(profile, real_manifests)
    gates = [s for s in stale if s.field == "quality_gates"]

    assert gates, [s.field for s in stale]
    assert gates[0].manifest_value == "static-analysis:phpstan-level-6"
    assert gates[0].profile_value == "static-analysis:psalm"


def test_a_gate_pinning_a_configuration_of_the_same_tool_is_not_drift(real_manifests):
    """The profile names `phpstan`; DB-001 pins `phpstan-level-6`. Same tool."""
    stale, _ = find_stale(MATCHING_PROFILE, real_manifests)
    assert not [s for s in stale if s.field == "quality_gates"]


def test_a_gate_the_manifest_omits_is_an_advisory_never_drift(real_manifests):
    """DB-004 deliberately carries no static-analysis gate. Authoring choice."""
    stale, advisories = find_stale(MATCHING_PROFILE, real_manifests)

    assert not [s for s in stale if s.id == "DB-004" and s.field == "quality_gates"]
    assert [a for a in advisories if a.id == "DB-004" and "static-analysis" in a.why]


# ---------------------------------------------------------------------
# Guard: the rule table against the schemas it speaks to
# ---------------------------------------------------------------------

def test_templates_stay_consistent_with_the_schemas():
    schema = json.loads(
        (Path(__file__).parent.parent / "src" / "acsdd" / "schemas"
         / "capability.schema.json").read_text()
    )
    categories = set(
        schema["properties"]["capability"]["properties"]["category"]["enum"])
    slugs = {t.slug for t in _TEMPLATES}

    for template in _TEMPLATES:
        assert template.category in categories, template.slug
        assert re.match(_CAP_ID_PATTERN, f"{template.category}-001"), template.category
        assert set(template.depends_on) <= slugs, template.slug
        assert template.what and template.why, template.slug


def _discover(repo: Path, files: dict) -> dict:
    from acsdd.profile import _discovery_impl

    repo.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (repo / name).write_text(content)
    (repo / "src").mkdir(exist_ok=True)

    _discovery_impl.main([str(repo), "--output", str(repo / "out"),
                          "--profile-id", "guard"])
    return yaml.safe_load((repo / "out" / "guard-draft.yaml").read_text())["profile"]


def _dotted_paths(node, prefix="") -> set:
    paths = set()
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.add(path)
            paths |= _dotted_paths(value, path)
    return paths


def test_every_template_path_resolves_against_a_real_discovered_profile(tmp_path):
    """The rule table keys off profile paths. If discovery renames or drops one,
    the trait silently stops firing and the recommendations quietly get worse —
    with nothing failing. This is what fails instead.

    Two fixture repos, unioned, because several stack fields are conditionally
    omitted: `technology_stack.frontend` only appears once there is a frontend
    to describe. A single-repo version of this test would call that field dead.
    """
    backend = _discover(tmp_path / "backend", {"composer.json": json.dumps({
        "require": {"php": "^8.1", "symfony/framework-bundle": "6.1.*",
                    "doctrine/orm": "^2.10"},
    })})
    frontend = _discover(tmp_path / "frontend", {"package.json": json.dumps({
        "dependencies": {"react": "^19.0.0", "react-dom": "^19.0.0"},
        "devDependencies": {"vite": "^7.0.0"},
    })})
    emitted = _dotted_paths(backend) | _dotted_paths(frontend)

    for template in _TEMPLATES:
        for path in list(template.requires) + [h.path for h in template.gate_hints]:
            assert path in emitted, (
                f"{template.slug} keys off '{path}', which discovery no longer "
                f"emits — the trait can never fire again")


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def _write_profile(path: Path, profile: dict) -> None:
    path.write_text(yaml.dump({"profile": profile}, default_flow_style=False,
                              sort_keys=False))


def test_recommend_cli_json_payload_is_complete(tmp_path):
    profile_path = tmp_path / "example.yaml"
    _write_profile(profile_path, MATCHING_PROFILE)

    result = CliRunner().invoke(cli, [
        "capability", "recommend", "--profile", str(profile_path),
        "--capabilities-dir", str(Path(".acsdd") / "capabilities"), "--json",
    ])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    for key in ("acsdd_version", "profile_path", "capabilities_dir", "profile_id",
                "recommended_count", "missing_count", "stale_count", "recommended",
                "existing_by_category", "stale", "advisories"):
        assert key in payload, key
    assert payload["stale"] == []
    assert payload["missing_count"] > 0

    entry = payload["recommended"][0]
    for key in ("slug", "category", "name", "what", "why", "suggested_id", "status",
                "covered_by", "triggered_by", "blocked_by", "depends_on",
                "generate_command"):
        assert key in entry, key


def test_recommend_cli_exits_0_with_gaps_and_drift(tmp_path):
    """Informational, not a gate — same contract as `profile review`. Wiring it
    into CI as a failure would break every repo mid-upgrade."""
    profile = copy.deepcopy(MATCHING_PROFILE)
    profile["technology_stack"]["additional"] = [{"name": "doctrine/orm", "version": "3.6.1"}]
    profile_path = tmp_path / "example.yaml"
    _write_profile(profile_path, profile)

    result = CliRunner().invoke(cli, [
        "capability", "recommend", "--profile", str(profile_path),
        "--capabilities-dir", str(Path(".acsdd") / "capabilities"),
    ])
    assert result.exit_code == 0, result.output
    assert "stale field(s)" in result.output
    assert "capability gap(s)" in result.output


def test_recommend_cli_without_a_profile_exits_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["capability", "recommend"])

    assert result.exit_code == 1
    assert "couldn't auto-detect one" in result.output


def test_recommend_cli_works_on_a_repo_with_no_manifests_yet(tmp_path, monkeypatch):
    """The first run of the command, which is the one that matters most: a
    finalized profile and nothing generated yet."""
    monkeypatch.chdir(tmp_path)
    profiles_dir = tmp_path / ".acsdd" / "profiles"
    profiles_dir.mkdir(parents=True)
    _write_profile(profiles_dir / "example.yaml", MATCHING_PROFILE)

    result = CliRunner().invoke(cli, ["capability", "recommend"])
    assert result.exit_code == 0, result.output
    assert "Identify Doctrine Entities" in result.output
    assert "acsdd capability generate --id DB-001 --category DB" in result.output
