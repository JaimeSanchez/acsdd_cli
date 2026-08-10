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
    installed_targets,
    is_installed,
    read_skill,
    remove_skill,
    resolve_targets,
    skill_names,
)


def test_skill_asset_loads_via_importlib_resources():
    content = read_skill("profile-review")
    assert content.startswith("---\n")
    assert "acsdd profile review --json" in content


def test_capability_plan_asset_loads_via_importlib_resources():
    content = read_skill("capability-plan")
    assert content.startswith("---\n")
    # Each skill drives itself off its own --json command rather than restating
    # what the CLI knows; losing that line is how a skill starts going stale.
    assert "acsdd capability recommend --json" in content


def test_c4_component_diagram_asset_loads_via_importlib_resources():
    content = read_skill("c4-component-diagram")
    assert content.startswith("---\n")
    # This skill has no --json partner to keep it honest (see the note in
    # acsdd/skills.py), so the strings that would rot silently are the
    # C4-PlantUML include and the four status tags the whole classification
    # scheme is built on.
    assert "!include <C4/C4_Component>" in content
    for tag in ("new", "modified", "related", "removed"):
        assert f'AddElementTag("{tag}"' in content


def test_graph_import_asset_loads_via_importlib_resources():
    content = read_skill("graph-import")
    assert content.startswith("---\n")
    # The whole design of this skill is that the vocabulary, the allowed-edge
    # matrix and the rule table come from the payload rather than being
    # restated here. Losing this line is how it starts inventing them.
    assert "acsdd graph context --json" in content
    # The commands it hands back to the user, which are the gate.
    assert "acsdd graph apply" in content
    assert "--dry-run" in content
    assert "acsdd graph validate" in content


def test_graph_import_refuses_to_carry_the_rule_tables_itself():
    """The vocabulary is published by `graph context --json` and versioned with
    the schema. A skill that listed the node types would go stale the first
    time one was added, and would go stale silently."""
    content = read_skill("graph-import")

    from acsdd.graph.vocabulary import EDGE_TYPES

    # A couple of edge-type names appear in prose as examples, which is fine;
    # what must not appear is the matrix — every edge type enumerated.
    named = [name for name in EDGE_TYPES if name in content]
    assert len(named) < len(EDGE_TYPES), (
        f"graph-import names every edge type ({named}) — it is restating the "
        f"matrix instead of reading it from `acsdd graph context --json`")


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


def test_install_writes_every_shipped_skill_for_every_agent(tmp_path):
    # The whole point of the target registry: a skill installed for one agent
    # and invisible to the next is the failure being prevented here.
    for name in SKILLS:
        results = install_skill(name, tmp_path)
        assert [r.target for r in results] == ["agents", "claude"]
        assert all(r.written for r in results)

        for root in (".agents", ".claude"):
            path = tmp_path / root / "skills" / name / "SKILL.md"
            assert path.read_text() == read_skill(name)


def test_install_can_be_narrowed_to_one_agent(tmp_path):
    results = install_skill("profile-review", tmp_path, targets=["agents"])
    assert [r.target for r in results] == ["agents"]
    assert (tmp_path / ".agents" / "skills" / "profile-review" / "SKILL.md").exists()
    assert not (tmp_path / ".claude").exists()

    assert is_installed("profile-review", tmp_path) is True
    assert is_installed("profile-review", tmp_path, targets=["claude"]) is False
    assert installed_targets("profile-review", tmp_path) == ["agents"]


def test_install_rejects_an_unknown_agent_target():
    with pytest.raises(SkillError) as exc:
        resolve_targets(["emacs"])
    assert "unknown agent target 'emacs'" in str(exc.value)


def test_install_refuses_to_clobber_without_force(tmp_path):
    target = tmp_path / ".claude" / "skills" / "profile-review" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("hand-edited\n")

    results = {r.target: r for r in install_skill("profile-review", tmp_path)}
    assert results["claude"].written is False
    assert target.read_text() == "hand-edited\n"
    # One blocked target must not cost the others theirs — a repo with a
    # hand-edited .claude/ should still end up readable by every other agent.
    assert results["agents"].written is True

    forced = {r.target: r for r in install_skill("profile-review", tmp_path, force=True)}
    assert forced["claude"].written is True
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

    assert "[ ] capability-plan" in before.output
    assert "[ ] c4-component-diagram" in before.output

    # Both agent conventions are named, so a reader can see which agents a
    # given repo's skills are actually reachable from.
    assert ".agents/skills/profile-review/SKILL.md" in before.output
    assert ".claude/skills/profile-review/SKILL.md" in before.output
    assert "Codex" in before.output and "Claude Code" in before.output

    runner.invoke(cli, ["skill", "install", "--dir", str(tmp_path)])
    after = runner.invoke(cli, ["skill", "list", "--dir", str(tmp_path)])
    assert "[x] profile-review" in after.output
    assert "[x] capability-plan" in after.output
    assert "[x] c4-component-diagram" in after.output


def test_skill_list_marks_a_partially_installed_skill_per_agent(tmp_path):
    install_skill("profile-review", tmp_path, targets=["claude"])
    output = CliRunner().invoke(cli, ["skill", "list", "--dir", str(tmp_path)]).output

    # Installed somewhere, so the skill reads as present...
    assert "[x] profile-review" in output
    # ...but the per-agent lines still show Codex/Cursor/Kimi can't see it.
    assert "[ ] .agents/skills/profile-review/SKILL.md" in output
    assert "[x] .claude/skills/profile-review/SKILL.md" in output


def test_skill_show_dumps_the_markdown():
    result = CliRunner().invoke(cli, ["skill", "show", "profile-review"])
    assert result.exit_code == 0, result.output
    assert "name: profile-review" in result.output


def test_remove_skill_reports_a_no_op_when_not_installed(tmp_path):
    results = remove_skill("profile-review", tmp_path)
    assert [r.target for r in results] == ["agents", "claude"]
    assert not any(r.removed for r in results)


def test_remove_skill_raises_on_an_unknown_name(tmp_path):
    with pytest.raises(SkillError):
        remove_skill("nope", tmp_path)


def test_skill_remove_cli_round_trip(tmp_path):
    runner = CliRunner()
    runner.invoke(cli, ["skill", "install", "--dir", str(tmp_path)])
    assert "[x] profile-review" in runner.invoke(
        cli, ["skill", "list", "--dir", str(tmp_path)]).output

    result = runner.invoke(cli, [
        "skill", "remove", "profile-review", "--dir", str(tmp_path), "--force",
    ])
    assert result.exit_code == 0, result.output
    assert "Removed" in result.output
    assert is_installed("profile-review", tmp_path) is False
    assert "[ ] profile-review" in runner.invoke(
        cli, ["skill", "list", "--dir", str(tmp_path)]).output


def test_skill_remove_refuses_without_force(tmp_path):
    runner = CliRunner()
    runner.invoke(cli, ["skill", "install", "--dir", str(tmp_path)])

    result = runner.invoke(cli, ["skill", "remove", "profile-review", "--dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "Would remove:" in result.output
    assert is_installed("profile-review", tmp_path) is True


def test_skill_remove_leaves_the_agent_skills_dirs_in_place(tmp_path):
    # Neither .claude/skills/ nor .agents/skills/ is acsdd's — pruning either
    # would delete other tools' skills along with ours.
    runner = CliRunner()
    runner.invoke(cli, ["skill", "install", "--dir", str(tmp_path)])
    runner.invoke(cli, ["skill", "remove", "profile-review", "--dir", str(tmp_path), "--force"])

    for root in (".agents", ".claude"):
        assert (tmp_path / root / "skills").is_dir()
        assert not (tmp_path / root / "skills" / "profile-review").exists()


def test_skill_remove_clears_every_agent_target(tmp_path):
    runner = CliRunner()
    runner.invoke(cli, ["skill", "install", "--dir", str(tmp_path)])

    result = runner.invoke(cli, [
        "skill", "remove", "profile-review", "--dir", str(tmp_path), "--force",
    ])
    assert result.exit_code == 0, result.output
    assert installed_targets("profile-review", tmp_path) == []
    # The other skills are untouched by a single-skill removal.
    assert installed_targets("capability-plan", tmp_path) == ["agents", "claude"]


def test_skill_remove_can_be_narrowed_to_one_agent(tmp_path):
    runner = CliRunner()
    runner.invoke(cli, ["skill", "install", "--dir", str(tmp_path)])

    result = runner.invoke(cli, [
        "skill", "remove", "profile-review", "--dir", str(tmp_path),
        "--agent", "claude", "--force",
    ])
    assert result.exit_code == 0, result.output
    assert installed_targets("profile-review", tmp_path) == ["agents"]


def test_skill_remove_on_an_uninstalled_skill_exits_1(tmp_path):
    result = CliRunner().invoke(cli, [
        "skill", "remove", "profile-review", "--dir", str(tmp_path), "--force",
    ])
    assert result.exit_code == 1
    assert "is not installed" in result.output


def test_skill_remove_rejects_an_unknown_name(tmp_path):
    result = CliRunner().invoke(cli, [
        "skill", "remove", "nope", "--dir", str(tmp_path), "--force",
    ])
    assert result.exit_code == 1
    assert "unknown skill 'nope'" in result.output
