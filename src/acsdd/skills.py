"""Installs the Claude Code skills acsdd ships into a consumer repository.

`acsdd profile review` explains what's still unresolved in a draft profile;
resolving it means reading the repo and weighing evidence, which is work for an
agent rather than a CLI. The skills here are that agent-facing half — procedure
only, with all per-field knowledge left in acsdd.profile.review so the two can't
drift.

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
from typing import Dict, List, Optional

from acsdd.removal import remove_paths


class SkillError(Exception):
    """Raised for an unknown skill name or an unreadable packaged asset."""


@dataclass(frozen=True)
class SkillAsset:
    name: str
    # Path within the acsdd.assets package.
    resource: str
    # Where it lands in the consumer repo, relative to the repo root.
    dest: PurePosixPath
    summary: str


@dataclass(frozen=True)
class InstallResult:
    name: str
    path: Path
    written: bool


@dataclass(frozen=True)
class RemoveResult:
    name: str
    path: Path
    # False when the skill wasn't installed here in the first place.
    removed: bool


SKILLS: Dict[str, SkillAsset] = {
    "profile-review": SkillAsset(
        name="profile-review",
        resource="claude/skills/profile-review/SKILL.md",
        dest=PurePosixPath(".claude/skills/profile-review/SKILL.md"),
        summary="Resolve the [REVIEW REQUIRED] fields in a draft engineering profile.",
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


def install_skill(name: str, repo_root: Path, force: bool = False) -> InstallResult:
    """Writes a skill into repo_root, creating parent directories as needed.

    Refuses to clobber an existing file unless `force` — .claude/ belongs to a
    different tool and is frequently hand-edited, so overwriting is always the
    caller's explicit choice.
    """
    asset = _lookup(name)
    content = read_skill(name)
    target = repo_root / asset.dest

    if target.exists() and not force:
        return InstallResult(name=name, path=target, written=False)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return InstallResult(name=name, path=target, written=True)


def remove_skill(name: str, repo_root: Path) -> RemoveResult:
    """Deletes an installed skill from repo_root.

    Mirrors install_skill: reports a no-op via the result rather than raising
    when the file isn't there, and still raises SkillError for a name acsdd
    doesn't ship. The emptied ``<name>/`` directory goes too, but
    ``.claude/skills/`` and ``.claude/`` never do — that tree is Claude Code's,
    and acsdd is only a guest in it.
    """
    asset = _lookup(name)
    target = repo_root / asset.dest

    if not target.exists():
        return RemoveResult(name=name, path=target, removed=False)

    # remove_paths only ever prunes a file's *immediate* parent, so this takes
    # the emptied `.claude/skills/<name>/` and stops there.
    remove_paths([target])
    return RemoveResult(name=name, path=target, removed=True)


def is_installed(name: str, repo_root: Path) -> bool:
    return (repo_root / _lookup(name).dest).exists()


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
