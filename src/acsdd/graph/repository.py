"""Reading and writing graphs, changesets, and the revision log.

The `capability.loader` charter: I/O only. Nothing here validates a schema or
checks an invariant — `graph.validator` and `graph.integrity` own that, and
`graph.applier` sequences the three. Keeping the split means a corrupt file and
an invalid graph fail in different places with different messages.

`GraphRepository` is a Protocol rather than a base class so a future
``Neo4jGraphRepository`` or ``RemoteGraphRepository`` can satisfy it without
inheriting a JSON implementation's assumptions. `JsonGraphRepository` is the
only one for now, and the MVP deliberately ships no graph database.

**Writes are atomic.** ``graph.json`` is the first large durable file this tool
rewrites — everything else creates or deletes — and a half-written one is worse
than any existing failure mode. Every write goes to a sibling temp file and
then `os.replace`, the same same-filesystem-rename trick `acsdd.update` uses to
swap a running binary.
"""

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Protocol

from acsdd import paths
from acsdd.graph.changeset import ChangeSetError, GraphChangeSet
from acsdd.graph.model import Graph, GraphLoadError, graph_hash, serialize


@dataclass(frozen=True)
class GraphRevision:
    """One entry in the revision log.

    The id is ``<sequence>-<hash prefix>`` — ``0007-4f2a91c3``. The sequence is
    for humans reading a pull request; the hash makes the id content-addressed,
    so two people applying the same changeset to the same base arrive at the
    same revision id and a divergence is detectable instead of silent.
    """

    id: str
    parent: Optional[str]
    changeset_id: Optional[str]
    title: str
    applied_at: str
    node_count: int
    edge_count: int
    graph_hash: str

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "parent": self.parent,
            "changeset_id": self.changeset_id,
            "title": self.title,
            "applied_at": self.applied_at,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "graph_hash": self.graph_hash,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "GraphRevision":
        return cls(
            id=data["id"],
            parent=data.get("parent"),
            changeset_id=data.get("changeset_id"),
            title=data.get("title", ""),
            applied_at=data.get("applied_at", ""),
            node_count=int(data.get("node_count", 0)),
            edge_count=int(data.get("edge_count", 0)),
            graph_hash=data.get("graph_hash", ""),
        )


def next_revision_id(sequence: int, digest: str) -> str:
    return f"{sequence:04d}-{digest[:8]}"


class GraphRepository(Protocol):
    """Where a graph lives. Implemented by `JsonGraphRepository` today."""

    def exists(self) -> bool: ...
    def load(self) -> Graph: ...
    def save(self, graph: Graph, revision: GraphRevision,
             acsdd_version: str = "", generated_at: Optional[date] = None) -> None: ...
    def revisions(self) -> List[GraphRevision]: ...


def _write_atomically(path: Path, text: str) -> None:
    """Write `text` to `path` via a sibling temp file and `os.replace`.

    Sibling rather than /tmp so the rename is same-filesystem and therefore
    atomic; `os.replace` overwrites on every platform acsdd targets. On any
    failure the temp file is removed and `path` is left exactly as it was.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                                    suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _read_json(path: Path, what: str) -> Dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise GraphLoadError(f"cannot read {what} at {path}: {e}") from e
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise GraphLoadError(f"{path} is not parseable JSON: {e}") from e


class JsonGraphRepository:
    """The durable repository graph as a checked-in JSON file."""

    def __init__(self, graph_dir: Path):
        self.graph_dir = graph_dir
        self.graph_path = graph_dir / paths.GRAPH_FILE
        self.revisions_dir = graph_dir / paths.REVISIONS_SUBDIR
        self.revision_index = self.revisions_dir / paths.REVISION_INDEX_FILE

    # -- reading --------------------------------------------------------
    def exists(self) -> bool:
        return self.graph_path.is_file()

    def load(self) -> Graph:
        """The graph on disk, or an empty one when the repo has none yet.

        An absent graph is a legitimate starting state, not an error: the first
        `graph apply` in a repository has nothing to read. A *corrupt* graph is
        an error, and raises.
        """
        if not self.exists():
            return Graph()
        return Graph.from_dict(_read_json(self.graph_path, "graph"))

    def load_document(self) -> Dict:
        """The raw document, for schema validation before it becomes a Graph."""
        return _read_json(self.graph_path, "graph")

    def revisions(self) -> List[GraphRevision]:
        if not self.revision_index.is_file():
            return []
        data = _read_json(self.revision_index, "revision index")
        entries = data.get("revisions") if isinstance(data, dict) else None
        return [GraphRevision.from_dict(e) for e in entries or []]

    def head(self) -> Optional[GraphRevision]:
        log = self.revisions()
        return log[-1] if log else None

    # -- writing --------------------------------------------------------
    def plan_revision(self, graph: Graph, changeset_id: Optional[str],
                      title: str, applied_at: Optional[date] = None) -> GraphRevision:
        """The revision a save of `graph` would produce, without saving it.

        Split out so `graph apply --dry-run` can report the revision id it
        would create — the same "what would happen" separation
        `capability.remover.plan_removal` draws.
        """
        log = self.revisions()
        digest = graph_hash(graph)
        applied = applied_at or datetime.now(timezone.utc).date()
        return GraphRevision(
            id=next_revision_id(len(log) + 1, digest),
            parent=log[-1].id if log else None,
            changeset_id=changeset_id,
            title=title,
            applied_at=applied.isoformat(),
            node_count=len(graph.nodes),
            edge_count=len(graph.edges),
            graph_hash=digest,
        )

    def save(self, graph: Graph, revision: GraphRevision, acsdd_version: str = "",
             generated_at: Optional[date] = None) -> None:
        """Write the graph, then append the revision entry.

        Order matters and is deliberate: an index entry pointing at a graph
        that was never written is a lie the next command believes, whereas a
        graph with no index entry is recoverable — its hash re-derives from its
        own content. So the cheap-to-repair failure is the one left possible.
        """
        graph.revision = revision.id
        _write_atomically(self.graph_path,
                          serialize(graph, generated_at=generated_at,
                                    acsdd_version=acsdd_version))

        log = self.revisions()
        log.append(revision)
        _write_atomically(
            self.revision_index,
            json.dumps({"revisions": [r.to_dict() for r in log]},
                       indent=2, sort_keys=False, ensure_ascii=False) + "\n")


# -- changes ------------------------------------------------------------

@dataclass(frozen=True)
class ChangeRecord:
    """The ``change.json`` beside a change's overlay and changeset."""

    id: str
    title: str
    created_at: str
    prd_path: Optional[str] = None

    def to_dict(self) -> Dict:
        out: Dict = {"id": self.id, "title": self.title, "created_at": self.created_at}
        if self.prd_path:
            out["prd_path"] = self.prd_path
        return {"change": out}

    @classmethod
    def from_dict(cls, data: Dict) -> "ChangeRecord":
        if not isinstance(data, dict) or not isinstance(data.get("change"), dict):
            raise GraphLoadError("document has no top-level 'change' mapping")
        body = data["change"]
        return cls(id=body["id"], title=body.get("title", ""),
                   created_at=body.get("created_at", ""),
                   prd_path=body.get("prd_path"))


class ChangeStore:
    """The per-change overlay directory: ``.acsdd/changes/<change-id>/``."""

    def __init__(self, changes_dir: Path):
        self.changes_dir = changes_dir

    def change_dir(self, change_id: str) -> Path:
        return self.changes_dir / change_id

    def record_path(self, change_id: str) -> Path:
        return self.change_dir(change_id) / paths.CHANGE_FILE

    def changeset_path(self, change_id: str) -> Path:
        return self.change_dir(change_id) / paths.CHANGESET_FILE

    def applied_path(self, change_id: str) -> Path:
        return self.change_dir(change_id) / paths.APPLIED_FILE

    def list_ids(self) -> List[str]:
        if not self.changes_dir.is_dir():
            return []
        return sorted(d.name for d in self.changes_dir.iterdir()
                      if d.is_dir() and (d / paths.CHANGE_FILE).is_file())

    def open_ids(self) -> List[str]:
        """Changes that have a record but no ``applied.json``.

        What `_default_change_id` auto-detects against. An applied change is
        history; writing a new changeset into one is almost never the intent.
        """
        return [cid for cid in self.list_ids() if not self.applied_path(cid).is_file()]

    def exists(self, change_id: str) -> bool:
        return self.record_path(change_id).is_file()

    def load_record(self, change_id: str) -> ChangeRecord:
        return ChangeRecord.from_dict(
            _read_json(self.record_path(change_id), "change record"))

    def save_record(self, record: ChangeRecord) -> Path:
        path = self.record_path(record.id)
        _write_atomically(path, json.dumps(record.to_dict(), indent=2,
                                           sort_keys=False, ensure_ascii=False) + "\n")
        return path

    def load_changeset(self, change_id: str) -> GraphChangeSet:
        return load_changeset(self.changeset_path(change_id))

    def save_changeset(self, changeset: GraphChangeSet) -> Path:
        path = self.changeset_path(changeset.id)
        _write_atomically(path, json.dumps(changeset.to_dict(), indent=2,
                                           sort_keys=False, ensure_ascii=False) + "\n")
        return path

    def mark_applied(self, change_id: str, revision_id: str,
                     applied_at: Optional[date] = None) -> Path:
        """Record that this change has landed.

        The first of the three idempotency layers: a literal re-run of
        ``graph apply`` on an already-applied change is caught here, before any
        operation is replayed.
        """
        applied = (applied_at or datetime.now(timezone.utc).date()).isoformat()
        path = self.applied_path(change_id)
        _write_atomically(path, json.dumps(
            {"applied": {"change_id": change_id, "revision": revision_id,
                         "applied_at": applied}},
            indent=2, sort_keys=False, ensure_ascii=False) + "\n")
        return path

    def applied_revision(self, change_id: str) -> Optional[str]:
        path = self.applied_path(change_id)
        if not path.is_file():
            return None
        data = _read_json(path, "applied record")
        return (data.get("applied") or {}).get("revision")


def load_changeset(path: Path) -> GraphChangeSet:
    """Read a changeset document. Raises `ChangeSetError` / `GraphLoadError`."""
    return GraphChangeSet.from_dict(_read_json(path, "changeset"))


def load_changeset_document(path: Path) -> Dict:
    """The raw document, for schema validation before it becomes a changeset."""
    return _read_json(path, "changeset")


__all__ = [
    "ChangeRecord", "ChangeSetError", "ChangeStore", "GraphRepository",
    "GraphRevision", "JsonGraphRepository", "load_changeset",
    "load_changeset_document", "next_revision_id",
]
