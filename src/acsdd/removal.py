"""Deleting acsdd's artifacts off disk.

Shared by every `remove` command. The commands themselves decide *what* to
delete — which files belong to a capability, a profile, or a skill — and each
of those questions lives with the thing it's about (``capability.remover``,
``paths.profile_artifact_paths``, ``skills.remove_skill``). What's common is
only the deletion itself, plus the one subtlety worth centralizing: cleaning up
directories the deletion emptied, without ever removing a directory some other
part of the tool expects to keep finding.
"""

from pathlib import Path
from typing import Iterable, List


def remove_paths(paths: Iterable[Path], protect: Iterable[Path] = ()) -> List[Path]:
    """Deletes each path, then prunes the directories it emptied.

    Returns what was actually removed; paths that don't exist are skipped
    rather than raising, so callers can pass a superset.

    `protect` names directories to leave alone even once empty. Two structural
    directories need it: ``_manifests/`` is how ``resolve_capabilities_dir``
    recognizes a capabilities tree at all, so removing the last capability must
    not take it away, and a profiles directory that vanished when its last
    profile did would read as "this repo was never onboarded".

    Pruning is best-effort and only ever touches a directory that is already
    empty — ``rmdir`` raises on a non-empty one, which is exactly the guard we
    want, so the error is swallowed rather than pre-checked. Only immediate
    parents are considered: emptying ``backend/`` shouldn't cascade upward and
    take the capabilities root with it.
    """
    protected = {Path(p).resolve() for p in protect}

    removed: List[Path] = []
    parents: List[Path] = []
    for path in paths:
        if not path.exists():
            continue
        path.unlink()
        removed.append(path)
        if path.parent not in parents:
            parents.append(path.parent)

    for parent in parents:
        if parent.resolve() in protected:
            continue
        try:
            parent.rmdir()
        except OSError:
            pass

    return removed
