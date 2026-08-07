"""Regression tests for acsdd.profile._discovery_impl.

These exercise the detectors directly against a synthetic fixture modeled on
a real Symfony 6.1 / Doctrine / PostgreSQL DDD-layered repository that was
used to spot-check acsdd against a target outside its own bundled example
data. That exercise found several concrete bugs (an architecture-pattern tie
silently resolved by dict-insertion order, a common PHPStan config filename
not recognized, a generated cache directory walked as if it were application
code, and no database-engine detection at all) — this file is the regression
net for those fixes.
"""

import json
import textwrap
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from acsdd.cli import cli
from acsdd.profile.generator import find_unresolved_fields
from acsdd.profile._discovery_impl import (
    ArchitectureDetector,
    ConventionDetector,
    DatabaseDetector,
    HealthMetrics,
    StackDetector,
    SymfonyStructureDetector,
    _is_excluded_dir,
)


def _build_symfony_ddd_fixture(root: Path) -> None:
    """A minimal Symfony 6.1 / Doctrine / PostgreSQL repo with a DDD-style
    src/ layout, mirroring the real repo that exposed these bugs."""
    (root / "config" / "packages").mkdir(parents=True)
    (root / "config" / "bundles.php").write_text("<?php\nreturn [];\n")
    (root / "public").mkdir()
    (root / "public" / "index.php").write_text("<?php\n")

    (root / "composer.json").write_text(json.dumps({
        "require": {
            "php": ">=8.1",
            "symfony/framework-bundle": "6.1.*",
            "doctrine/orm": "^3.6",
            "doctrine/doctrine-bundle": "^2.13",
        }
    }))
    (root / "composer.lock").write_text(json.dumps({
        "packages": [
            {"name": "symfony/framework-bundle", "version": "v6.1.11"},
            {"name": "doctrine/orm", "version": "3.6.2"},
            {"name": "doctrine/doctrine-bundle", "version": "2.13.1"},
            {"name": "doctrine/dbal", "version": "3.7.2"},
        ],
        "packages-dev": [
            {"name": "doctrine/doctrine-migrations-bundle", "version": "v3.3.1"},
        ],
    }))
    (root / "symfony.lock").write_text(json.dumps({
        "symfony/framework-bundle": {"version": "6.1", "recipe": {"repo": "github.com/symfony/recipes"}},
        "doctrine/doctrine-bundle": {"version": "2.11", "recipe": {"repo": "github.com/symfony/recipes"}},
    }))
    (root / "phpstan.dist.neon").write_text("parameters:\n    level: 8\n")

    src = root / "src"
    for name in ("Application", "Domain", "Infrastructure", "Controller", "Entity", "Repository"):
        d = src / name
        d.mkdir(parents=True)
        (d / "Placeholder.php").write_text("<?php\n")

    # Leftover cache output from an unrelated tool run — not application
    # code, and previously walked as if it were.
    graphify_cache = src / "graphify-out" / "cache" / "ast"
    graphify_cache.mkdir(parents=True)
    (graphify_cache / "junk.json").write_text("{}")

    (root / "docker-compose.yml").write_text(textwrap.dedent("""\
        services:
          database:
            image: postgres:13-alpine
        """))

    (root / "README.md").write_text("# Test fixture\n")


def test_stack_detection_php_symfony(tmp_path):
    _build_symfony_ddd_fixture(tmp_path)
    by_role = StackDetector(tmp_path).detect_by_role()
    assert "backend" in by_role
    stack, _, confidence = by_role["backend"]
    assert stack == "php-symfony"
    assert confidence > 0


def test_symfony_structure_detector_prefers_composer_lock_resolved_version(tmp_path):
    _build_symfony_ddd_fixture(tmp_path)
    info = SymfonyStructureDetector(tmp_path).detect()
    # composer.json only declares "6.1.*"; composer.lock resolved v6.1.11 —
    # the exact resolved version should win over the constraint-regexed one.
    assert info["version"] == "6.1.11"
    assert info["version_constraint"] == "6.1.*"


def test_symfony_structure_detector_populates_additional_packages_from_lock(tmp_path):
    _build_symfony_ddd_fixture(tmp_path)
    info = SymfonyStructureDetector(tmp_path).detect()
    additional = {pkg["name"]: pkg["version"] for pkg in info["additional_packages"]}
    assert additional == {
        "doctrine/orm": "3.6.2",
        "doctrine/dbal": "3.7.2",
        "doctrine/doctrine-bundle": "2.13.1",
        "doctrine/doctrine-migrations-bundle": "3.3.1",
    }


def test_symfony_structure_detector_flex_from_symfony_lock_alone(tmp_path):
    # No config/packages, config/bundles.php, or public/index.php — only
    # symfony.lock, which Flex always writes and should be enough on its own.
    (tmp_path / "composer.json").write_text(json.dumps({
        "require": {"symfony/framework-bundle": "6.1.*"}
    }))
    (tmp_path / "symfony.lock").write_text("{}")
    info = SymfonyStructureDetector(tmp_path).detect()
    assert info["structure"] == "flex"


def test_symfony_structure_detector_falls_back_without_composer_lock(tmp_path):
    (tmp_path / "composer.json").write_text(json.dumps({
        "require": {"symfony/framework-bundle": "^6.0"}
    }))
    info = SymfonyStructureDetector(tmp_path).detect()
    assert info["version"] == "6.0"
    assert info["additional_packages"] == []


def test_symfony_structure_detector_ignores_malformed_composer_lock(tmp_path):
    (tmp_path / "composer.json").write_text(json.dumps({
        "require": {"symfony/framework-bundle": "^6.0"}
    }))
    (tmp_path / "composer.lock").write_text("not valid json")
    info = SymfonyStructureDetector(tmp_path).detect()
    assert info["version"] == "6.0"
    assert info["additional_packages"] == []


def test_architecture_detector_reports_tie_not_silent_winner(tmp_path):
    _build_symfony_ddd_fixture(tmp_path)
    pattern, confidence = ArchitectureDetector(tmp_path).detect()
    # Controller/Entity/Repository (layered) and Domain/Application/
    # Infrastructure (hexagonal) score identically for this layout — both
    # must be reported rather than one silently picked by dict order.
    assert set(pattern.split("+")) == {"layered", "hexagonal"}
    assert confidence > 0


def test_is_excluded_dir_covers_generated_output_dirs():
    assert _is_excluded_dir("graphify-out")
    assert not _is_excluded_dir("Domain")
    assert not _is_excluded_dir("Controller")


def test_convention_detector_finds_phpstan_dist_neon(tmp_path):
    _build_symfony_ddd_fixture(tmp_path)
    conventions = ConventionDetector(tmp_path).detect()
    tools = {f["tool"] for f in conventions["static_analysis"]}
    assert "phpstan" in tools


def test_database_detector_from_docker_compose_postgres(tmp_path):
    _build_symfony_ddd_fixture(tmp_path)
    assert DatabaseDetector(tmp_path).detect() == "postgresql"


def test_database_detector_from_env_database_url(tmp_path):
    (tmp_path / ".env").write_text('DATABASE_URL="mysql://user:pass@127.0.0.1:3306/app"\n')
    assert DatabaseDetector(tmp_path).detect() == "mysql"


def test_database_detector_returns_none_when_undetectable(tmp_path):
    assert DatabaseDetector(tmp_path).detect() is None


def test_profile_discover_refuses_overwrite_without_force(tmp_path):
    _build_symfony_ddd_fixture(tmp_path)
    output_dir = tmp_path / "profiles"
    runner = CliRunner()

    first = runner.invoke(cli, [
        "profile", "discover", str(tmp_path),
        "--profile-id", "test-profile", "--output", str(output_dir),
    ])
    assert first.exit_code == 0, first.output

    second = runner.invoke(cli, [
        "profile", "discover", str(tmp_path),
        "--profile-id", "test-profile", "--output", str(output_dir),
    ])
    assert second.exit_code != 0
    assert "--force" in second.output

    forced = runner.invoke(cli, [
        "profile", "discover", str(tmp_path),
        "--profile-id", "test-profile", "--output", str(output_dir),
        "--force",
    ])
    assert forced.exit_code == 0, forced.output


def test_profile_discover_defaults_profile_id_to_repo_dir_name(tmp_path):
    repo_dir = tmp_path / "my-cool-project"
    repo_dir.mkdir()
    _build_symfony_ddd_fixture(repo_dir)
    output_dir = tmp_path / "profiles"
    runner = CliRunner()

    result = runner.invoke(cli, [
        "profile", "discover", str(repo_dir), "--output", str(output_dir),
    ])
    assert result.exit_code == 0, result.output
    assert "using directory name: my-cool-project" in result.output
    assert (output_dir / "my-cool-project-draft.yaml").exists()


def test_profile_discover_surfaces_doctrine_additional_packages(tmp_path):
    _build_symfony_ddd_fixture(tmp_path)
    output_dir = tmp_path / "profiles"
    runner = CliRunner()

    result = runner.invoke(cli, [
        "profile", "discover", str(tmp_path),
        "--profile-id", "additional-test", "--output", str(output_dir),
    ])
    assert result.exit_code == 0, result.output

    draft = yaml.safe_load((output_dir / "additional-test-draft.yaml").read_text())
    tech_stack = draft["profile"]["technology_stack"]
    assert tech_stack["version"] == "6.1.11 (symfony/framework-bundle: 6.1.*)"
    additional = {pkg["name"]: pkg["version"] for pkg in tech_stack["additional"]}
    assert additional["doctrine/orm"] == "3.6.2"
    assert additional["doctrine/dbal"] == "3.7.2"


def test_profile_discover_depth_surface_skips_architecture_and_health(tmp_path):
    _build_symfony_ddd_fixture(tmp_path)
    output_dir = tmp_path / "profiles"
    runner = CliRunner()

    result = runner.invoke(cli, [
        "profile", "discover", str(tmp_path),
        "--profile-id", "surface-test", "--output", str(output_dir),
        "--depth", "surface",
    ])
    assert result.exit_code == 0, result.output

    report = (output_dir / "surface-test-discovery-report.md").read_text()
    assert "[REVIEW REQUIRED — run --depth deep]" in report

    # The surface-mode placeholder in tech_debt_score must still pass schema
    # validation — it's not one of the low/medium/high enum members, so this
    # is a regression test for the schema's placeholder-acceptance pattern.
    validate = runner.invoke(cli, [
        "profile", "validate", str(output_dir / "surface-test-draft.yaml"),
    ])
    assert validate.exit_code == 0, validate.output


def test_profile_validate_strict_flags_unresolved_placeholders(tmp_path):
    _build_symfony_ddd_fixture(tmp_path)
    output_dir = tmp_path / "profiles"
    runner = CliRunner()

    result = runner.invoke(cli, [
        "profile", "discover", str(tmp_path),
        "--profile-id", "strict-test", "--output", str(output_dir),
    ])
    assert result.exit_code == 0, result.output
    draft_path = output_dir / "strict-test-draft.yaml"

    lax = runner.invoke(cli, ["profile", "validate", str(draft_path)])
    assert lax.exit_code == 0, lax.output

    strict = runner.invoke(cli, ["profile", "validate", str(draft_path), "--strict"])
    assert strict.exit_code != 0
    assert "unresolved [REVIEW REQUIRED]" in strict.output
    assert "engineering_standards.code_style" in strict.output


def test_review_covers_every_placeholder_real_discovery_emits(tmp_path):
    # The drift detector. review.py's guidance table is written by hand against
    # _discovery_impl's emission sites; adding a new placeholder there without a
    # matching guidance entry silently degrades `profile review` to echoing a
    # raw placeholder. This fails the suite instead.
    _build_symfony_ddd_fixture(tmp_path)
    output_dir = tmp_path / "profiles"
    runner = CliRunner()

    result = runner.invoke(cli, [
        "profile", "discover", str(tmp_path),
        "--profile-id", "drift-test", "--output", str(output_dir),
    ])
    assert result.exit_code == 0, result.output
    draft_path = output_dir / "drift-test-draft.yaml"

    review = runner.invoke(cli, [
        "profile", "review", str(draft_path), "--json", "--repo-path", str(tmp_path),
    ])
    assert review.exit_code == 0, review.output
    payload = json.loads(review.stdout)

    profile = yaml.safe_load(draft_path.read_text())["profile"]
    expected = find_unresolved_fields(profile)
    assert expected, "the fixture should still produce placeholders"
    assert payload["unresolved_count"] == len(expected)
    assert [u["path"] for u in payload["unresolved"]] == expected

    ungoverned = [u["path"] for u in payload["unresolved"] if not u["has_guidance"]]
    assert ungoverned == [], (
        f"no guidance registered in acsdd.profile.review for: {ungoverned}"
    )


def test_low_confidence_frontend_is_flagged_as_an_unresolved_placeholder(tmp_path):
    # A bare package.json with no react/react-dom dependency scores 2 (the
    # signature file alone), i.e. confidence 0.4 — below the 0.6 threshold, so
    # ProfileGenerator marks technology_stack.frontend for review. That
    # placeholder used to be *appended* to the detected value, which meant
    # find_unresolved_fields' startswith() match never saw it and the field
    # sailed through both `profile validate --strict` and `profile create`.
    _build_symfony_ddd_fixture(tmp_path)
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"lodash": "^4.17.21"},
    }))
    output_dir = tmp_path / "profiles"
    runner = CliRunner()

    result = runner.invoke(cli, [
        "profile", "discover", str(tmp_path),
        "--profile-id", "frontend-test", "--output", str(output_dir),
    ])
    assert result.exit_code == 0, result.output

    draft_path = output_dir / "frontend-test-draft.yaml"
    profile = yaml.safe_load(draft_path.read_text())["profile"]
    frontend = profile["technology_stack"]["frontend"]
    assert frontend.startswith("[REVIEW REQUIRED")
    # The detected value is preserved inside the placeholder, not discarded.
    assert "react" in frontend

    strict = runner.invoke(cli, ["profile", "validate", str(draft_path), "--strict"])
    assert strict.exit_code != 0
    assert "technology_stack.frontend" in strict.output


def test_health_metrics_parses_cobertura_coverage_xml(tmp_path):
    (tmp_path / "coverage.xml").write_text(
        '<?xml version="1.0"?><coverage line-rate="0.876"></coverage>'
    )
    metrics = HealthMetrics(tmp_path).analyze()
    assert metrics["coverage"] == pytest.approx(87.6)


def test_health_metrics_parses_clover_xml(tmp_path):
    (tmp_path / "clover.xml").write_text(
        '<?xml version="1.0"?>'
        '<coverage><project>'
        '<metrics statements="200" coveredstatements="150"/>'
        '</project></coverage>'
    )
    metrics = HealthMetrics(tmp_path).analyze()
    assert metrics["coverage"] == pytest.approx(75.0)
