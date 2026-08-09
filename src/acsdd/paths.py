"""Where acsdd's artifacts live inside a consumer repository.

Everything acsdd writes into a repo lives under a single hidden ``.acsdd/``
root — ``.acsdd/profiles/`` and ``.acsdd/capabilities/``. The one deliberate
exception is ``.claude/skills/`` (see ``acsdd.skills``): that path is Claude
Code's convention, not acsdd's, so it stays where that tool expects it.

Earlier versions used a visible ``./acsdd/`` for profiles and ``./capabilities/``
at the repo root. Both are still *found* — the resolvers below fall back to them
and report it, so an already-onboarded repo keeps working — but nothing ever
*writes* to a legacy location again. New output always lands under ``.acsdd/``.

This module is the only place either layout is spelled out. Don't hardcode
``capabilities`` or ``.acsdd`` anywhere else; call a resolver.
"""

from pathlib import Path
from typing import Tuple

# The hidden root every acsdd artifact lives under.
ACSDD_DIR = ".acsdd"

PROFILES_SUBDIR = "profiles"
CAPABILITIES_SUBDIR = "capabilities"
MANIFESTS_SUBDIR = "_manifests"

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
