"""Finalizes a human-reviewed draft profile into an active one.

`profile discover` produces a draft with everything it could auto-detect
filled in and everything it couldn't left as an explicit `[REVIEW
REQUIRED...]` placeholder. Nothing gates promotion of that draft into a
profile the rest of the toolchain should actually trust — `acsdd profile
validate` only checks schema shape, and a profile full of unresolved
placeholders passes that check exactly as well as a completed one. This
module is the gate: it refuses to finalize a profile until every placeholder
has been resolved by a human.
"""

from copy import deepcopy
from datetime import date
from typing import Dict, List

REVIEW_PREFIX = "[REVIEW REQUIRED"

# The exact draft defaults ProfileGenerator.generate() stamps on every
# profile discover run — only bump these on finalize if they're still
# untouched; a human who already set a custom version is left alone.
_DRAFT_VERSION = "0.1.0"
_ACTIVE_VERSION = "1.0.0"


def find_unresolved_fields(profile: Dict) -> List[str]:
    """Recursively scans a profile dict for string values that still start
    with the [REVIEW REQUIRED...] placeholder prefix, returning a sorted
    list of dotted/indexed paths (e.g. "technology_stack.database",
    "security_profile.cross_cutting_constraints[0].tool")."""
    found: List[str] = []

    def walk(value, path: str):
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                walk(child, f"{path}[{i}]")
        elif isinstance(value, str) and value.startswith(REVIEW_PREFIX):
            found.append(path)

    walk(profile, "")
    return sorted(found)


def finalize_profile(profile: Dict) -> Dict:
    """Assumes the caller already confirmed find_unresolved_fields() is
    empty. Returns a copy with meta.status flipped to active, meta.version
    bumped from the draft default (only if still untouched), and
    meta.last_updated refreshed."""
    result = deepcopy(profile)
    meta = result.setdefault("meta", {})
    meta["status"] = "active"
    if meta.get("version") == _DRAFT_VERSION:
        meta["version"] = _ACTIVE_VERSION
    meta["last_updated"] = date.today().isoformat()
    return result
