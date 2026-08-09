"""Deletes a capability's on-disk artifacts — the inverse of `capability
generate`.

Generation writes a *pair* of files in two different directories: the manifest
under ``_manifests/`` and a procedure-doc stub under the category's doc folder.
Removal has to find and delete both, and the doc half can't be located by
filename — see ``catalog.builder.find_doc_file``, which this module reuses.

The dependency check is the reason this isn't a two-line ``unlink``. Deleting a
manifest that others depend on leaves dangling refs that ``validate_catalog``
flags on every subsequent ``capability validate`` and ``catalog verify``, so
callers are expected to refuse rather than warn.

This module only works out *what* to delete; the deletion itself is
``acsdd.removal.remove_paths``, shared with the other remove commands.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from acsdd.catalog.builder import find_doc_file


class CapabilityNotFoundError(Exception):
    """Raised when the requested capability id has no manifest."""


@dataclass(frozen=True)
class RemovalPlan:
    capability_id: str
    manifest_path: Path
    # None when no procedure doc could be matched to this capability.
    doc_path: Optional[Path]
    # [(capability_id, name), ...] of manifests that depend on this one.
    dependents: List[Tuple[str, str]]

    @property
    def paths(self) -> List[Path]:
        """The files that would actually be deleted, existing ones only."""
        candidates = [self.manifest_path, self.doc_path]
        return [p for p in candidates if p is not None and p.exists()]


def find_dependents(manifests: Dict[str, Dict], cap_id: str) -> List[Tuple[str, str]]:
    """Reverse dependency lookup: who lists `cap_id` in their dependencies.

    Walks the same edge set as ``validator.validate_catalog``, so a non-empty
    result here is exactly the set of dangling refs removal would create.
    """
    dependents: List[Tuple[str, str]] = []
    for other_id, data in manifests.items():
        if other_id == cap_id:
            continue
        cap = data.get("capability", {}) or {}
        for dep in cap.get("dependencies", []) or []:
            if (dep or {}).get("capability") == cap_id:
                dependents.append((other_id, cap.get("name") or "?"))
                break
    return sorted(dependents)


def plan_removal(capabilities_dir: Path, manifests_dir: Path,
                 manifests: Dict[str, Dict], cap_id: str) -> RemovalPlan:
    """Works out what removing `cap_id` would delete and what would break.

    `manifests` is the already-loaded {id: raw_dict} mapping the caller built
    (see cli.py's `_load_all`), so this never re-reads the tree.
    """
    if cap_id not in manifests:
        raise CapabilityNotFoundError(cap_id)

    cap = manifests[cap_id].get("capability", {}) or {}
    category = cap.get("category") or ""

    return RemovalPlan(
        capability_id=cap_id,
        manifest_path=manifests_dir / f"{cap_id}.yaml",
        doc_path=find_doc_file(cap_id, category, capabilities_dir, manifests_dir),
        dependents=find_dependents(manifests, cap_id),
    )
