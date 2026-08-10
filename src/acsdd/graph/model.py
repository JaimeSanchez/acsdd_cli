"""The graph domain model: nodes, edges, their provenance, and the canonical
serialization that makes ``graph.json`` reviewable in a pull request.

Two things here are load-bearing beyond the obvious.

**Edge ids are derived, never authored** (see `edge_id`). That buys idempotency
for free — re-adding the same edge is the same key — a stable diff across
re-imports, and an id a reviewer can check against the vocabulary matrix
without a lookup.

**Serialization is canonical.** ``graph.json`` is a checked-in file that grows
to thousands of lines and gets reviewed by humans in diffs, so byte stability
matters as much as correctness: sorted arrays rather than id-keyed objects,
omitted empties, and an injectable date rather than a timestamp. The rules are
spelled out on `Graph.to_dict`. `catalog.builder.build_catalog_markdown` set
this precedent with its ``generated_at`` parameter and for the same reason.

Unlike `capability.recommender`, whose dicts are terminal output, everything
here round-trips: the graph is persisted and re-read on every command. So
`to_dict` and `from_dict` are written adjacent, always, and
`tests/test_graph_model.py` enforces the pair — split them across modules and
they drift within one release.

No filesystem access. `graph.repository` owns that.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

from acsdd.graph import vocabulary

# The version of the on-disk graph document format, written to graph.meta.
# Bumped when the shape changes in a way an older acsdd could not read.
GRAPH_FORMAT_VERSION = "1.0.0"


class GraphLoadError(Exception):
    """Raised when a graph document cannot be turned into a `Graph`.

    The `capability.loader.ManifestLoadError` analogue: structural failures
    that stop parsing dead, as opposed to the findings `graph.validator` and
    `graph.integrity` collect and report.
    """


def _today() -> date:
    return datetime.now(timezone.utc).date()


@dataclass(frozen=True)
class GraphEvidence:
    """A citation into the repository or a source document.

    This is the field that separates a finding from a guess. An agent-authored
    node with no evidence is exactly what the `no-evidence` advisory exists to
    surface, so keep the shape cheap enough that there is no excuse to omit it.
    """

    path: str
    lines: Optional[str] = None   # "42" or "40-52"
    quote: Optional[str] = None   # verbatim, <= 200 chars
    note: Optional[str] = None

    def to_dict(self) -> Dict:
        out: Dict = {"path": self.path}
        if self.lines:
            out["lines"] = self.lines
        if self.quote:
            out["quote"] = self.quote
        if self.note:
            out["note"] = self.note
        return out

    @classmethod
    def from_dict(cls, data: Dict) -> "GraphEvidence":
        return cls(
            path=data["path"],
            lines=data.get("lines"),
            quote=data.get("quote"),
            note=data.get("note"),
        )

    def sort_key(self) -> Tuple[str, str]:
        return (self.path, self.lines or "")


@dataclass(frozen=True)
class GraphSource:
    """Where a node or edge came from.

    `at` is a **date, not a timestamp**, deliberately. A timestamp changes on
    every re-run and turns graph.json into a file whose diff is pure noise and
    which nobody therefore reviews — the same reason CATALOG.md carries only a
    generated date.
    """

    kind: str                      # vocabulary.SOURCE_KINDS
    ref: str                       # "docs/prd/checkout.md" | "acsdd profile discover" | "user"
    at: str                        # "YYYY-MM-DD"
    agent: Optional[str] = None    # "claude-code/graph-import"
    acsdd_version: Optional[str] = None

    def to_dict(self) -> Dict:
        out: Dict = {"kind": self.kind, "ref": self.ref, "at": self.at}
        if self.agent:
            out["agent"] = self.agent
        if self.acsdd_version:
            out["acsdd_version"] = self.acsdd_version
        return out

    @classmethod
    def from_dict(cls, data: Dict) -> "GraphSource":
        return cls(
            kind=data["kind"],
            ref=data["ref"],
            at=data["at"],
            agent=data.get("agent"),
            acsdd_version=data.get("acsdd_version"),
        )


@dataclass(frozen=True)
class GraphNode:
    id: str
    type: str
    name: str
    summary: str = ""
    attributes: Dict[str, str] = field(default_factory=dict)
    evidence: List[GraphEvidence] = field(default_factory=list)
    source: Optional[GraphSource] = None
    confidence: int = 100
    status: str = "active"

    @property
    def layer(self) -> str:
        """Derived from `type`, never stored.

        A stored layer can contradict its type (``type: Component`` with
        ``layer: business``) and would need its own integrity rule to police
        the contradiction. Deriving it deletes the rule.

        Raises KeyError for an unknown type — callers that may hold one check
        `graph.integrity`'s `unknown-type` finding first, rather than having
        this silently answer "business".
        """
        return vocabulary.layer_of(self.type)

    def to_dict(self) -> Dict:
        out: Dict = {"id": self.id, "type": self.type, "name": self.name}
        if self.summary:
            out["summary"] = self.summary
        if self.attributes:
            out["attributes"] = {k: self.attributes[k] for k in sorted(self.attributes)}
        if self.evidence:
            out["evidence"] = [e.to_dict()
                               for e in sorted(self.evidence, key=GraphEvidence.sort_key)]
        if self.source is not None:
            out["source"] = self.source.to_dict()
        if self.confidence != 100:
            out["confidence"] = self.confidence
        if self.status != "active":
            out["status"] = self.status
        return out

    @classmethod
    def from_dict(cls, data: Dict) -> "GraphNode":
        return cls(
            id=data["id"],
            type=data["type"],
            name=data["name"],
            summary=data.get("summary", ""),
            attributes=dict(data.get("attributes") or {}),
            evidence=[GraphEvidence.from_dict(e) for e in data.get("evidence") or []],
            source=GraphSource.from_dict(data["source"]) if data.get("source") else None,
            confidence=int(data.get("confidence", 100)),
            status=data.get("status", "active"),
        )


def edge_id(from_id: str, edge_type: str, to_id: str) -> str:
    """``<from>|<type>|<to>``.

    Derived rather than authored, for three reasons that all pay off:

    - **Idempotency comes free.** Re-applying a changeset that adds the same
      edge collapses onto the same key instead of accumulating duplicates.
    - **The diff is stable.** Two runs of the same import produce the same ids,
      so re-importing a revised PRD shows only what actually changed.
    - **A reviewer can read it.** ``cap:BE-001|implemented_by|cmp:checkout``
      states the whole edge, and can be checked against the vocabulary matrix
      without cross-referencing anything.

    The cost is that you cannot hold two edges of the same type between the
    same pair carrying different rationales. That is a feature: two rationales
    for one relationship is one rationale written badly.
    """
    return f"{from_id}|{edge_type}|{to_id}"


@dataclass(frozen=True)
class GraphEdge:
    id: str                        # always edge_id(from_id, type, to_id)
    type: str
    from_id: str
    to_id: str
    rationale: str = ""
    evidence: List[GraphEvidence] = field(default_factory=list)
    source: Optional[GraphSource] = None
    confidence: int = 100

    @classmethod
    def create(cls, from_id: str, edge_type: str, to_id: str, **kwargs) -> "GraphEdge":
        """The constructor to prefer — derives the id so no caller has to know
        the format. `__init__` still takes an explicit id because `from_dict`
        needs to round-trip whatever is on disk, and because a mismatch there
        is a finding (`edge-id-derivation`) rather than a parse failure."""
        return cls(id=edge_id(from_id, edge_type, to_id), type=edge_type,
                   from_id=from_id, to_id=to_id, **kwargs)

    def to_dict(self) -> Dict:
        out: Dict = {"id": self.id, "type": self.type,
                     "from": self.from_id, "to": self.to_id}
        if self.rationale:
            out["rationale"] = self.rationale
        if self.evidence:
            out["evidence"] = [e.to_dict()
                               for e in sorted(self.evidence, key=GraphEvidence.sort_key)]
        if self.source is not None:
            out["source"] = self.source.to_dict()
        if self.confidence != 100:
            out["confidence"] = self.confidence
        return out

    @classmethod
    def from_dict(cls, data: Dict) -> "GraphEdge":
        from_id = data["from"]
        to_id = data["to"]
        edge_type = data["type"]
        return cls(
            id=data.get("id") or edge_id(from_id, edge_type, to_id),
            type=edge_type,
            from_id=from_id,
            to_id=to_id,
            rationale=data.get("rationale", ""),
            evidence=[GraphEvidence.from_dict(e) for e in data.get("evidence") or []],
            source=GraphSource.from_dict(data["source"]) if data.get("source") else None,
            confidence=int(data.get("confidence", 100)),
        )


@dataclass
class Graph:
    """A set of nodes and edges.

    Mutable container, frozen contents — `capability.recommender`'s
    `RecommendationReport` shape. Treat it as immutable by convention: every
    mutation goes through `graph.changeset.apply_operations`, which returns a
    new Graph rather than editing one in place. That is what lets
    ``graph apply --dry-run`` be the real apply with the write left off.
    """

    nodes: Dict[str, GraphNode] = field(default_factory=dict)
    edges: Dict[str, GraphEdge] = field(default_factory=dict)
    revision: Optional[str] = None

    # -- traversal ------------------------------------------------------
    def out_edges(self, node_id: str, edge_type: Optional[str] = None) -> List[GraphEdge]:
        return sorted(
            (e for e in self.edges.values()
             if e.from_id == node_id and (edge_type is None or e.type == edge_type)),
            key=lambda e: e.id)

    def in_edges(self, node_id: str, edge_type: Optional[str] = None) -> List[GraphEdge]:
        return sorted(
            (e for e in self.edges.values()
             if e.to_id == node_id and (edge_type is None or e.type == edge_type)),
            key=lambda e: e.id)

    def neighbours(self, node_id: str) -> List[str]:
        """Adjacent node ids in both directions, deduplicated and sorted."""
        found = {e.to_id for e in self.out_edges(node_id)}
        found |= {e.from_id for e in self.in_edges(node_id)}
        found.discard(node_id)
        return sorted(found)

    def nodes_of_type(self, node_type: str) -> List[GraphNode]:
        return sorted((n for n in self.nodes.values() if n.type == node_type),
                      key=lambda n: n.id)

    def merged_with(self, other: "Graph") -> "Graph":
        """This graph overlaid with `other`'s nodes and edges.

        How a change overlay is read against the repo graph: the overlay wins
        on a collision, because the overlay is the thing being proposed. The
        revision stays this graph's — an overlay has no revision of its own.
        """
        merged = Graph(nodes=dict(self.nodes), edges=dict(self.edges),
                       revision=self.revision)
        merged.nodes.update(other.nodes)
        merged.edges.update(other.edges)
        return merged

    # -- serialization --------------------------------------------------
    def to_dict(self, generated_at: Optional[date] = None,
                acsdd_version: str = "", graph_hash: Optional[str] = None) -> Dict:
        """The canonical on-disk document.

        Determinism rules, all of which exist so the file diffs cleanly in a
        pull request:

        1. Keys are emitted in declared order (never ``sort_keys``), matching
           the two existing ``--json`` payloads.
        2. `nodes` and `edges` are **arrays sorted by id**, not objects keyed
           by id. An object prints every id twice, and neither diff tools nor
           merge tools respect JSON object ordering; a sorted array gives a
           clean line diff on insert.
        3. Within a record, ``attributes`` keys are sorted and ``evidence`` is
           sorted by ``(path, lines)``.
        4. Empty collections and defaulted fields are **omitted**, not written
           as ``[]``/``null``. A departure from `recommender.to_dict`, which
           writes every key: a 20-line report wants a fixed shape for its
           consumer, a 3000-line file wants short records a human can scan.
           `from_dict` supplies the defaults.
        5. `generated_at` is injectable — `build_catalog_markdown`'s signature
           verbatim — so re-serializing an unchanged graph is a no-op diff and
           tests can byte-compare.
        """
        meta: Dict = {
            "version": GRAPH_FORMAT_VERSION,
            "generated_at": (generated_at or _today()).isoformat(),
        }
        if acsdd_version:
            meta["acsdd_version"] = acsdd_version
        if self.revision:
            meta["revision"] = self.revision
        if graph_hash:
            meta["graph_hash"] = graph_hash

        return {
            "graph": {
                "meta": meta,
                "nodes": [self.nodes[i].to_dict() for i in sorted(self.nodes)],
                "edges": [self.edges[i].to_dict() for i in sorted(self.edges)],
            }
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "Graph":
        """Parse a graph document.

        Raises `GraphLoadError` on a duplicate id rather than letting the dict
        keying swallow the second record — silently dropping a node is exactly
        the class of bug the `id-unique` integrity rule exists to catch, and it
        must not be un-catchable because parsing already destroyed the evidence.
        """
        if not isinstance(data, dict) or not isinstance(data.get("graph"), dict):
            raise GraphLoadError("document has no top-level 'graph' mapping")

        body = data["graph"]
        graph = cls(revision=(body.get("meta") or {}).get("revision"))

        for raw in body.get("nodes") or []:
            try:
                node = GraphNode.from_dict(raw)
            except (KeyError, TypeError) as e:
                raise GraphLoadError(f"unreadable node {raw!r}: {e}") from e
            if node.id in graph.nodes:
                raise GraphLoadError(f"duplicate node id '{node.id}'")
            graph.nodes[node.id] = node

        for raw in body.get("edges") or []:
            try:
                edge = GraphEdge.from_dict(raw)
            except (KeyError, TypeError) as e:
                raise GraphLoadError(f"unreadable edge {raw!r}: {e}") from e
            if edge.id in graph.edges:
                raise GraphLoadError(f"duplicate edge id '{edge.id}'")
            graph.edges[edge.id] = edge

        return graph


def serialize(graph: Graph, generated_at: Optional[date] = None,
              acsdd_version: str = "") -> str:
    """The canonical bytes for a graph, hash included, ending in a newline.

    The one place a graph becomes text. `graph.repository` writes exactly what
    this returns, and `tests/test_graph_model.py` asserts running it twice — or
    parsing its output and running it again — produces identical bytes.
    """
    payload = graph.to_dict(generated_at=generated_at, acsdd_version=acsdd_version,
                            graph_hash=graph_hash(graph))
    return json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def graph_hash(graph: Graph) -> str:
    """sha256 over the node and edge content, with `meta` excluded.

    Excluded because meta carries the hash itself (self-reference) and the
    generation date (which would make an unchanged graph hash differently
    tomorrow). What is left is exactly the content two people would need to
    agree on to have the same graph.
    """
    body = graph.to_dict()["graph"]
    canonical = json.dumps({"nodes": body["nodes"], "edges": body["edges"]},
                           indent=None, sort_keys=False,
                           separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
