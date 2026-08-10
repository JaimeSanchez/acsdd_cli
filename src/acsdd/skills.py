"""Installs the agent skills acsdd ships into a consumer repository.

`acsdd profile review` explains what's still unresolved in a draft profile;
resolving it means reading the repo and weighing evidence, which is work for an
agent rather than a CLI. The skills here are that agent-facing half — procedure
only, with all per-field knowledge left in acsdd.profile.review so the two can't
drift.

`c4-component-diagram` is the deliberate exception: it has no CLI partner and
carries its own domain knowledge, because C4-PlantUML's macros and the status
colour standard come from outside this repo and don't move when acsdd's
detectors do. There'd be nothing for a Python table to own.

Nothing about a SKILL.md is vendor-specific — the format is YAML frontmatter
(`name`, `description`) over markdown procedure, and the procedure here drives
`acsdd ... --json`, which every agent can run. Only the *install path* differs
per agent, which is what AGENT_TARGETS is for: `.agents/skills/` is the shared
project-level convention (Codex, Cursor, Kimi CLI, Gemini CLI, Copilot,
OpenCode, Amp, Cline, Warp, …) and `.claude/skills/` is Claude Code's own. Both
are written by default, because a skill installed for one agent and invisible to
the next is the failure this registry exists to prevent.

The two copies are independent files rather than a symlink pair: symlinks are
unreliable on Windows and under `core.symlinks=false`, and the source of truth
is the packaged asset either way — `skill install --force` re-syncs both from
it. Copies going out of step with each other is not a state worth engineering
around.

Assets are read through importlib.resources, never a path relative to
``__file__``, so they resolve the same way from a source install and from the
frozen PyInstaller binary. Any new file added under ``acsdd/assets/`` must be
registered in *both* ``[tool.setuptools.package-data]`` (pyproject.toml) and the
``datas`` list in ``packaging/acsdd.spec`` — setuptools package-data is invisible
to PyInstaller, so missing the second one 404s at runtime in the binary while
working fine under pytest.
"""

from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Tuple

from acsdd.removal import remove_paths


class SkillError(Exception):
    """Raised for an unknown skill name or an unreadable packaged asset."""


@dataclass(frozen=True)
class AgentTarget:
    """One agent convention for where repo-local skills live."""
    name: str
    # Repo-root-relative directory the agent reads skills from.
    root: PurePosixPath
    # Which agents actually read it, for `skill list`.
    reads: str


AGENT_TARGETS: Dict[str, AgentTarget] = {
    "agents": AgentTarget(
        name="agents",
        root=PurePosixPath(".agents/skills"),
        reads="Codex, Cursor, Kimi CLI, Gemini CLI, Copilot, OpenCode, Amp, Cline, Warp",
    ),
    "claude": AgentTarget(
        name="claude",
        root=PurePosixPath(".claude/skills"),
        reads="Claude Code",
    ),
}

# Order matters only for output: the cross-agent path first, since it serves
# every agent but one.
DEFAULT_TARGETS: Tuple[str, ...] = ("agents", "claude")


@dataclass(frozen=True)
class SkillAsset:
    name: str
    # Path within the acsdd.assets package.
    resource: str
    summary: str

    @property
    def relpath(self) -> PurePosixPath:
        """Where it lands under an agent target's root."""
        return PurePosixPath(self.name) / "SKILL.md"

    def dest(self, target: AgentTarget) -> PurePosixPath:
        """Where it lands in the consumer repo, relative to the repo root."""
        return target.root / self.relpath


@dataclass(frozen=True)
class InstallResult:
    name: str
    target: str
    path: Path
    written: bool


@dataclass(frozen=True)
class RemoveResult:
    name: str
    target: str
    path: Path
    # False when the skill wasn't installed there in the first place.
    removed: bool


SKILLS: Dict[str, SkillAsset] = {
    "profile-review": SkillAsset(
        name="profile-review",
        resource="skills/profile-review/SKILL.md",
        summary="Resolve the [REVIEW REQUIRED] fields in a draft engineering profile.",
    ),
    "capability-plan": SkillAsset(
        name="capability-plan",
        resource="skills/capability-plan/SKILL.md",
        summary=("Decide which capabilities this repo should have, and update the "
                 "manifests a profile change left behind."),
    ),
    "c4-component-diagram": SkillAsset(
        name="c4-component-diagram",
        resource="skills/c4-component-diagram/SKILL.md",
        summary=("Diagram the architectural impact of a planned change: what's "
                 "new, modified, removed, or merely involved."),
    ),
}


def read_skill(name: str) -> str:
    """Returns a packaged skill's markdown source."""
    asset = _lookup(name)
    try:
        return (resources.files("acsdd.assets")
                .joinpath(asset.resource)
                .read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError) as exc:  # pragma: no cover - packaging bug
        raise SkillError(
            f"packaged asset for '{name}' is missing ({asset.resource}) — this is a "
            f"packaging bug; see the note in acsdd/skills.py"
        ) from exc


def install_skill(name: str, repo_root: Path, force: bool = False,
                  targets: Optional[Iterable[str]] = None) -> List[InstallResult]:
    """Writes a skill into repo_root once per agent target.

    Refuses to clobber an existing file unless `force` — those directories
    belong to other tools and are frequently hand-edited, so overwriting is
    always the caller's explicit choice. One target being blocked never stops
    the others: a repo that already hand-edited `.claude/skills/` should still
    end up with a readable `.agents/skills/` copy.
    """
    asset = _lookup(name)
    content = read_skill(name)
    results = []

    for target in resolve_targets(targets):
        path = repo_root / asset.dest(target)
        if path.exists() and not force:
            results.append(InstallResult(name, target.name, path, written=False))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        results.append(InstallResult(name, target.name, path, written=True))

    return results


def remove_skill(name: str, repo_root: Path,
                 targets: Optional[Iterable[str]] = None) -> List[RemoveResult]:
    """Deletes an installed skill from repo_root, per agent target.

    Mirrors install_skill: reports a no-op via the result rather than raising
    when the file isn't there, and still raises SkillError for a name acsdd
    doesn't ship. The emptied ``<name>/`` directory goes too, but
    ``.agents/skills/`` and ``.claude/skills/`` never do — those trees belong to
    other tools, and acsdd is only a guest in them.
    """
    asset = _lookup(name)
    results = []

    for target in resolve_targets(targets):
        path = repo_root / asset.dest(target)
        if not path.exists():
            results.append(RemoveResult(name, target.name, path, removed=False))
            continue
        # remove_paths only ever prunes a file's *immediate* parent, so this
        # takes the emptied `<root>/<name>/` and stops there.
        remove_paths([path])
        results.append(RemoveResult(name, target.name, path, removed=True))

    return results


def skill_paths(name: str, repo_root: Path,
                targets: Optional[Iterable[str]] = None) -> List[Tuple[AgentTarget, Path]]:
    """Every (target, path) pair a skill occupies, installed or not."""
    asset = _lookup(name)
    return [(t, repo_root / asset.dest(t)) for t in resolve_targets(targets)]


def is_installed(name: str, repo_root: Path,
                 targets: Optional[Iterable[str]] = None) -> bool:
    """True when the skill is installed for *any* of the given targets."""
    return any(path.exists() for _, path in skill_paths(name, repo_root, targets))


def installed_targets(name: str, repo_root: Path) -> List[str]:
    """Names of the targets this skill is currently installed for."""
    return [t.name for t, path in skill_paths(name, repo_root) if path.exists()]


def resolve_targets(names: Optional[Iterable[str]] = None) -> List[AgentTarget]:
    """Agent targets by name, in DEFAULT_TARGETS order. None (or 'all') = every one."""
    if names is None:
        return [AGENT_TARGETS[n] for n in DEFAULT_TARGETS]

    wanted = set()
    for name in names:
        if name == "all":
            wanted.update(DEFAULT_TARGETS)
            continue
        if name not in AGENT_TARGETS:
            known = ", ".join(sorted(AGENT_TARGETS)) or "(none)"
            raise SkillError(f"unknown agent target '{name}' — available: {known}, all")
        wanted.add(name)

    if not wanted:
        return [AGENT_TARGETS[n] for n in DEFAULT_TARGETS]
    return [AGENT_TARGETS[n] for n in DEFAULT_TARGETS if n in wanted]


def target_names() -> List[str]:
    return list(DEFAULT_TARGETS)


def _lookup(name: str) -> SkillAsset:
    try:
        return SKILLS[name]
    except KeyError:
        known = ", ".join(sorted(SKILLS)) or "(none)"
        raise SkillError(f"unknown skill '{name}' — available: {known}") from None


def skill_names() -> List[str]:
    return sorted(SKILLS)


def find_skill(name: Optional[str]) -> List[SkillAsset]:
    """All shipped skills when name is None, otherwise just the named one."""
    if name is None:
        return [SKILLS[n] for n in skill_names()]
    return [_lookup(name)]
