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

from click.testing import CliRunner

from acsdd.cli import cli
from acsdd.profile._discovery_impl import (
    ArchitectureDetector,
    ConventionDetector,
    DatabaseDetector,
    StackDetector,
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
    (root / "symfony.lock").write_text("{}")
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
