"""Tests for acsdd.skills and the `acsdd skill` command group.

test_skill_asset_loads_via_importlib_resources is the source-install half of
CLAUDE.md's two-path requirement. The frozen half can't be covered here — it
needs an actual PyInstaller build — and is asserted by the smoke-test step in
.github/workflows/release.yml instead.
"""

import pytest
import yaml
from click.testing import CliRunner

from acsdd.cli import cli
from acsdd.skills import (
    SKILLS,
    SkillError,
    install_skill,
    is_installed,
    read_skill,
    skill_names,
)


def test_skill_asset_loads_via_importlib_resources():
    content = read_skill("profile-review")
    assert content.startswith("---\n")
    assert "acsdd profile review --json" in content


def test_unknown_skill_raises_with_the_available_names():
    with pytest.raises(SkillError) as exc:
        read_skill("does-not-exist")
    assert "profile-review" in str(exc.value)


@pytest.mark.parametrize("name", skill_names())
def test_skill_frontmatter_is_wellformed(name):
    content = read_skill(name)
    _, frontmatter, _ = content.split("---\n", 2)
    parsed = yaml.safe_load(frontmatter)

    assert parsed["name"] == name, "frontmatter name must match the asset directory"
    assert parsed["description"].strip()
    # Third-person and trigger-rich — this text is what decides whether the
    # skill ever fires.
    assert "Use when" in parsed["description"]


def test_install_writes_the_skill_into_the_repo(tmp_path):
    assert is_installed("profile-review", tmp_path) is False

    result = install_skill("profile-review", tmp_path)
    assert result.written is True
    assert result.path == tmp_path / ".claude" / "skills" / "profile-review" / "SKILL.md"
    assert result.path.read_text() == read_skill("profile-review")
    assert is_installed("profile-review", tmp_path) is True


def test_install_refuses_to_clobber_without_force(tmp_path):
    target = tmp_path / ".claude" / "skills" / "profile-review" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("hand-edited\n")

    assert install_skill("profile-review", tmp_path).written is False
    assert target.read_text() == "hand-edited\n"

    assert install_skill("profile-review", tmp_path, force=True).written is True
    assert target.read_text() != "hand-edited\n"


def test_skill_install_cli_round_trip(tmp_path):
    runner = CliRunner()

    first = runner.invoke(cli, ["skill", "install", "--dir", str(tmp_path)])
    assert first.exit_code == 0, first.output
    assert "Wrote" in first.output

    second = runner.invoke(cli, ["skill", "install", "--dir", str(tmp_path)])
    assert second.exit_code == 1
    assert "already exists — re-run with --force" in second.output

    forced = runner.invoke(cli, ["skill", "install", "--dir", str(tmp_path), "--force"])
    assert forced.exit_code == 0, forced.output


def test_skill_install_rejects_an_unknown_name(tmp_path):
    result = CliRunner().invoke(cli, ["skill", "install", "nope", "--dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "unknown skill 'nope'" in result.output


def test_skill_list_shows_shipped_skills_and_install_state(tmp_path):
    runner = CliRunner()

    before = runner.invoke(cli, ["skill", "list", "--dir", str(tmp_path)])
    assert before.exit_code == 0, before.output
    for name in SKILLS:
        assert name in before.output
    assert "[ ] profile-review" in before.output

    runner.invoke(cli, ["skill", "install", "--dir", str(tmp_path)])
    after = runner.invoke(cli, ["skill", "list", "--dir", str(tmp_path)])
    assert "[x] profile-review" in after.output


def test_skill_show_dumps_the_markdown():
    result = CliRunner().invoke(cli, ["skill", "show", "profile-review"])
    assert result.exit_code == 0, result.output
    assert "name: profile-review" in result.output
