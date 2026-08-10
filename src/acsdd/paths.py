"""Where acsdd's artifacts live inside a consumer repository.

Everything acsdd writes into a repo lives under a single hidden ``.acsdd/``
root — ``.acsdd/profiles/``, ``.acsdd/capabilities/``, ``.acsdd/graph/`` and
``.acsdd/changes/``. The one deliberate exception is ``.claude/skills/`` (see
``acsdd.skills``): that path is Claude Code's convention, not acsdd's, so it
stays where that tool expects it.

Earlier versions used a visible ``./acsdd/`` for profiles and ``./capabilities/``
at the repo root. Both are still *found* — the resolvers below fall back to them
and report it, so an already-onboarded repo keeps working — but nothing ever
*writes* to a legacy location again. New output always lands under ``.acsdd/``.

This module is the only place either layout is spelled out. Don't hardcode
``capabilities`` or ``.acsdd`` anywhere else; call a resolver.
"""

from pathlib import Path
from typing import List, Tuple

# The hidden root every acsdd artifact lives under.
ACSDD_DIR = ".acsdd"

PROFILES_SUBDIR = "profiles"
CAPABILITIES_SUBDIR = "capabilities"
MANIFESTS_SUBDIR = "_manifests"
GRAPH_SUBDIR = "graph"
CHANGES_SUBDIR = "changes"

# Filenames inside the graph and change subtrees.
GRAPH_FILE = "graph.json"
REVISIONS_SUBDIR = "revisions"
REVISION_INDEX_FILE = "index.json"
CHANGE_FILE = "change.json"
CHANGESET_FILE = "changeset.json"
APPLIED_FILE = "applied.json"

# Pre-.acsdd locations, kept resolvable for repos onboarded before the move.
LEGACY_PROFILES_DIR = Path("acsdd") / PROFILES_SUBDIR
LEGACY_CAPABILITIES_DIR = Path(CAPABILITIES_SUBDIR)

# What `profile discover --output` defaults to. Always the new layout —
# discovery never writes into a legacy directory, even when one exists.
DEFAULT_PROFILES_DIR = f"./{ACSDD_DIR}/{PROFILES_SUBDIR}"


def resolve_profiles_dir(cwd: Path) -> Tuple[Path, bool]:
    """Returns (profiles_dir, is_legacy) for `cwd`.

    Deliberately cwd-relative with no walk-up: this has to match
    `profile discover --output`'s default exactly, and that default is
    relative to wherever the user ran it from.

    Prefers ``.acsdd/profiles``; falls back to the legacy ``acsdd/profiles``
    only when that exists and the new one doesn't. When neither exists it
    returns the ``.acsdd`` path — callers treat a missing directory as
    "nothing to auto-detect", and it's also the right place to write.
    """
    current = cwd / ACSDD_DIR / PROFILES_SUBDIR
    if current.is_dir():
        return current, False

    legacy = cwd / LEGACY_PROFILES_DIR
    if legacy.is_dir():
        return legacy, True

    return current, False


def resolve_capabilities_dir(cwd: Path) -> Tuple[Path, bool]:
    """Returns (capabilities_dir, is_legacy) for `cwd`.

    Walks up from `cwd` so the CLI works from any subdirectory of a repo that
    has adopted the layout. Both candidates are checked at *each* level before
    ascending — checking every level for ``.acsdd`` first and only then
    falling back to a second full walk for the legacy layout would let a
    distant ancestor's ``.acsdd/capabilities`` win over the legacy
    ``capabilities/`` of the repo you're actually standing in.

    Falls back to ``cwd/.acsdd/capabilities`` — where `capability generate`
    should create one if the repo has none yet.
    """
    for candidate in [cwd, *cwd.parents]:
        current = candidate / ACSDD_DIR / CAPABILITIES_SUBDIR
        if (current / MANIFESTS_SUBDIR).is_dir():
            return current, False

        legacy = candidate / LEGACY_CAPABILITIES_DIR
        if (legacy / MANIFESTS_SUBDIR).is_dir():
            return legacy, True

    return cwd / ACSDD_DIR / CAPABILITIES_SUBDIR, False


def resolve_acsdd_root(cwd: Path) -> Path:
    """Returns the ``.acsdd`` directory the graph subsystem should work in.

    Walks up like `resolve_capabilities_dir` rather than staying cwd-relative
    like `resolve_profiles_dir`: the graph is repo-scoped, so running
    ``acsdd graph validate`` from a subdirectory has to find the same graph as
    running it from the root. A root is recognized by an existing ``graph/`` or
    ``changes/`` subtree; failing that, by an existing ``.acsdd`` with either of
    the older subtrees in it, so a repo that has only been through
    `profile discover` still anchors on its own ``.acsdd`` instead of a parent's.

    Unlike its two siblings this returns a bare Path, not a ``(Path, bool)``
    pair. There is no legacy layout to fall back to — the graph postdates the
    move to ``.acsdd`` — and an is_legacy flag that is structurally always
    False is a lie every caller would have to read past.

    Falls back to ``cwd/.acsdd``, which is where a first graph should be
    created.
    """
    for candidate in [cwd, *cwd.parents]:
        root = candidate / ACSDD_DIR
        if (root / GRAPH_SUBDIR).is_dir() or (root / CHANGES_SUBDIR).is_dir():
            return root
        if (root / PROFILES_SUBDIR).is_dir() or (root / CAPABILITIES_SUBDIR).is_dir():
            return root

    return cwd / ACSDD_DIR


def resolve_graph_dir(cwd: Path) -> Path:
    """Where the durable repository graph lives. See `resolve_acsdd_root`."""
    return resolve_acsdd_root(cwd) / GRAPH_SUBDIR


def resolve_changes_dir(cwd: Path) -> Path:
    """Where per-change overlays live.

    Deliberately derived from the *same* root `resolve_graph_dir` used rather
    than running its own walk-up. Two independent walks would let a distant
    ancestor's ``changes/`` pair with the local ``graph/`` — the same class of
    bug `resolve_capabilities_dir` documents in its own docstring.
    """
    return resolve_acsdd_root(cwd) / CHANGES_SUBDIR


def change_artifact_paths(changes_dir: Path, change_id: str) -> List[Path]:
    """Every file that belongs to one change, whether or not it exists yet.

    The `profile_artifact_paths` analogue: "which files are this change" is a
    layout question, so it lives here rather than being spelled out at each
    site that needs the set. `change remove` wants all three.
    """
    change_dir = changes_dir / change_id
    return [
        change_dir / CHANGE_FILE,
        change_dir / CHANGESET_FILE,
        change_dir / APPLIED_FILE,
    ]


def profile_artifact_paths(profiles_dir: Path, profile_id: str,
                           include_finalized: bool = True) -> List[Path]:
    """Every file that belongs to one profile, whether or not it exists yet.

    `profile discover` writes three of these and `profile create` the fourth,
    so "which files are this profile" is a layout question and belongs here
    rather than being spelled out at each site that needs the set.

    `include_finalized=False` drops ``<id>.yaml`` and leaves exactly what
    discovery itself writes — that command's overwrite guard has to stay blind
    to a finalized profile sitting alongside the draft.
    """
    paths = [
        profiles_dir / f"{profile_id}-draft.yaml",
        profiles_dir / f"{profile_id}-discovery-report.md",
        profiles_dir / f"{profile_id}-recommendations.md",
    ]
    if include_finalized:
        paths.append(profiles_dir / f"{profile_id}.yaml")
    return paths
