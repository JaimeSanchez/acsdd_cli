"""Changesets: the proposal half of the graph.

A `Graph` is a *state*; a `GraphChangeSet` is a *proposal against* one. Keeping
them in separate modules — with the dependency pointing only this way — is what
lets ``graph apply --dry-run`` be the real apply with the write left off:
`apply_operations` is pure, returns a new graph, and never touches the disk.
`graph.applier` adds the transaction around it.

`ApplyOutcome` answers *what applying would do* before anything happens, which
is `capability.remover.plan_removal`'s charter applied to a mutation rather
than a deletion.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from acsdd.graph.model import Graph, GraphEdge, GraphNode, GraphSource, edge_id

OPERATIONS = ("add_node", "update_node", "remove_node", "add_edge", "remove_edge")

# Node fields an update_node operation may overwrite. `id` and `type` are
# absent deliberately: changing either makes it a different node, and the way
# to say that is remove plus add, where the integrity rules get a look at both.
UPDATABLE_NODE_FIELDS = ("name", "summary", "attributes", "evidence",
                         "source", "confidence", "status")


class ChangeSetError(Exception):
    """A changeset that cannot be turned into operations at all.

    Distinct from an operation that is *refused* — an unknown op or a
    remove_node naming a node that isn't there are findings from
    `apply_operations`, reported and survivable. This is for documents that
    don't parse.
    """


@dataclass(frozen=True)
class GraphOperation:
    op: str
    target_id: str
    node: Optional[GraphNode] = None
    edge: Optional[GraphEdge] = None
    fields: Dict[str, object] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> Dict:
        out: Dict = {"op": self.op}
        if self.node is not None:
            out["node"] = self.node.to_dict()
        elif self.edge is not None:
            out["edge"] = self.edge.to_dict()
        else:
            out["id"] = self.target_id
        if self.fields:
            out["fields"] = self.fields
        if self.reason:
            out["reason"] = self.reason
        return out

    @classmethod
    def from_dict(cls, data: Dict) -> "GraphOperation":
        op = data.get("op")
        if op not in OPERATIONS:
            raise ChangeSetError(f"unknown operation '{op}'")

        node = GraphNode.from_dict(data["node"]) if data.get("node") else None
        edge = GraphEdge.from_dict(data["edge"]) if data.get("edge") else None

        if op == "add_node":
            if node is None:
                raise ChangeSetError("add_node without a 'node'")
            target = node.id
        elif op == "add_edge":
            if edge is None:
                raise ChangeSetError("add_edge without an 'edge'")
            target = edge.id
        else:
            target = data.get("id") or ""
            if not target:
                raise ChangeSetError(f"{op} without an 'id'")

        return cls(op=op, target_id=target, node=node, edge=edge,
                   fields=dict(data.get("fields") or {}),
                   reason=data.get("reason", ""))


@dataclass(frozen=True)
class GraphChangeSet:
    id: str
    title: str
    operations: List[GraphOperation]
    base_revision: Optional[str] = None
    source: Optional[GraphSource] = None
    acsdd_version: str = ""

    def to_dict(self) -> Dict:
        out: Dict = {"id": self.id, "title": self.title}
        if self.base_revision is not None:
            out["base_revision"] = self.base_revision
        if self.acsdd_version:
            out["acsdd_version"] = self.acsdd_version
        if self.source is not None:
            out["source"] = self.source.to_dict()
        out["operations"] = [o.to_dict() for o in self.operations]
        return {"changeset": out}

    @classmethod
    def from_dict(cls, data: Dict) -> "GraphChangeSet":
        if not isinstance(data, dict) or not isinstance(data.get("changeset"), dict):
            raise ChangeSetError("document has no top-level 'changeset' mapping")

        body = data["changeset"]
        for required in ("id", "title"):
            if not body.get(required):
                raise ChangeSetError(f"changeset has no '{required}'")

        return cls(
            id=body["id"],
            title=body["title"],
            operations=[GraphOperation.from_dict(o) for o in body.get("operations") or []],
            base_revision=body.get("base_revision"),
            source=GraphSource.from_dict(body["source"]) if body.get("source") else None,
            acsdd_version=body.get("acsdd_version", ""),
        )


@dataclass(frozen=True)
class ApplyOutcome:
    """What applying would do. No I/O has happened when this is returned."""

    graph: Graph
    added_nodes: List[str] = field(default_factory=list)
    updated_nodes: List[str] = field(default_factory=list)
    removed_nodes: List[str] = field(default_factory=list)
    added_edges: List[str] = field(default_factory=list)
    removed_edges: List[str] = field(default_factory=list)
    no_ops: List[str] = field(default_factory=list)
    refusals: List[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not (self.added_nodes or self.updated_nodes or self.removed_nodes
                    or self.added_edges or self.removed_edges)

    @property
    def touched_nodes(self) -> List[str]:
        return sorted(set(self.added_nodes) | set(self.updated_nodes)
                      | set(self.removed_nodes))

    def to_dict(self) -> Dict:
        return {
            "added_nodes": self.added_nodes,
            "updated_nodes": self.updated_nodes,
            "removed_nodes": self.removed_nodes,
            "added_edges": self.added_edges,
            "removed_edges": self.removed_edges,
            "no_ops": self.no_ops,
            "refusals": self.refusals,
            "is_noop": self.is_noop,
        }


def _replace_node(node: GraphNode, changes: Dict[str, object]) -> GraphNode:
    """A copy of `node` with the named fields overwritten.

    Unknown keys are ignored rather than raising: an agent sending a field this
    version doesn't know about should not lose the fields it got right, and the
    ignored key surfaces as a refusal on the outcome.
    """
    payload = node.to_dict()
    for key, value in changes.items():
        if key in UPDATABLE_NODE_FIELDS:
            payload[key] = value
    # to_dict() omits defaults, so anything the update cleared has to come back
    # through from_dict()'s defaults rather than being silently retained.
    return GraphNode.from_dict(payload)


def apply_operations(graph: Graph, changeset: GraphChangeSet) -> ApplyOutcome:
    """Apply `changeset` to a copy of `graph` and report what changed.

    Pure — `graph` is never mutated, and nothing is read from or written to the
    filesystem. Every operation whose effect is already present lands in
    `no_ops` rather than being counted as a change, which is what makes
    re-running an import idempotent; every operation that cannot be carried out
    lands in `refusals` rather than raising, so one bad line in a large
    agent-authored changeset yields a report instead of a traceback.

    Ordering is the caller's: operations run in the order given. Adding an edge
    before its endpoints exist is allowed here and caught by the
    `dangling-edge` integrity rule against the final graph, so an agent doesn't
    have to topologically sort its own output.
    """
    result = Graph(nodes=dict(graph.nodes), edges=dict(graph.edges),
                   revision=graph.revision)
    added_nodes: List[str] = []
    updated_nodes: List[str] = []
    removed_nodes: List[str] = []
    added_edges: List[str] = []
    removed_edges: List[str] = []
    no_ops: List[str] = []
    refusals: List[str] = []

    for op in changeset.operations:
        if op.op == "add_node":
            node = op.node
            existing = result.nodes.get(node.id)
            if existing == node:
                no_ops.append(f"add_node {node.id}: already present and identical")
                continue
            if existing is not None:
                # An add over an existing node is an update in disguise. Treat
                # it as one rather than refusing: re-importing a revised PRD
                # re-emits its nodes wholesale, and demanding the agent
                # remember which ones it had already sent would make every
                # second import a diffing exercise.
                result.nodes[node.id] = node
                updated_nodes.append(node.id)
                continue
            result.nodes[node.id] = node
            added_nodes.append(node.id)

        elif op.op == "update_node":
            existing = result.nodes.get(op.target_id)
            if existing is None:
                refusals.append(f"update_node {op.target_id}: no such node")
                continue
            unknown = sorted(set(op.fields) - set(UPDATABLE_NODE_FIELDS))
            if unknown:
                refusals.append(
                    f"update_node {op.target_id}: ignored unknown field(s) "
                    f"{', '.join(unknown)}")
            updated = _replace_node(existing, op.fields)
            if updated == existing:
                no_ops.append(f"update_node {op.target_id}: no field changed")
                continue
            result.nodes[op.target_id] = updated
            updated_nodes.append(op.target_id)

        elif op.op == "remove_node":
            if op.target_id not in result.nodes:
                no_ops.append(f"remove_node {op.target_id}: not present")
                continue
            del result.nodes[op.target_id]
            removed_nodes.append(op.target_id)
            # Edges to a removed node would be dangling, which is an integrity
            # error. Removing them here rather than reporting them keeps
            # "delete this component" from requiring the agent to enumerate
            # every edge that touched it.
            for eid in [e.id for e in result.edges.values()
                        if e.from_id == op.target_id or e.to_id == op.target_id]:
                del result.edges[eid]
                removed_edges.append(eid)

        elif op.op == "add_edge":
            edge = op.edge
            canonical = edge_id(edge.from_id, edge.type, edge.to_id)
            if edge.id != canonical:
                # The id is derived; a disagreeing one means the author built
                # it by hand and may have built it wrong. Take the derived one
                # and say so rather than storing an id that lies.
                refusals.append(
                    f"add_edge {edge.from_id}->{edge.to_id}: supplied id "
                    f"'{edge.id}' is not the derived '{canonical}'; using the derived one")
                edge = GraphEdge(
                    id=canonical, type=edge.type, from_id=edge.from_id, to_id=edge.to_id,
                    rationale=edge.rationale, evidence=edge.evidence,
                    source=edge.source, confidence=edge.confidence)
            existing = result.edges.get(canonical)
            if existing == edge:
                no_ops.append(f"add_edge {canonical}: already present and identical")
                continue
            result.edges[canonical] = edge
            if existing is None:
                added_edges.append(canonical)
            else:
                no_ops.append(f"add_edge {canonical}: already present, metadata replaced")

        elif op.op == "remove_edge":
            if op.target_id not in result.edges:
                no_ops.append(f"remove_edge {op.target_id}: not present")
                continue
            del result.edges[op.target_id]
            removed_edges.append(op.target_id)

        else:  # pragma: no cover — GraphOperation.from_dict rejects these first
            refusals.append(f"unknown operation '{op.op}'")

    return ApplyOutcome(
        graph=result,
        added_nodes=added_nodes, updated_nodes=updated_nodes,
        removed_nodes=removed_nodes, added_edges=added_edges,
        removed_edges=sorted(set(removed_edges)), no_ops=no_ops, refusals=refusals)
