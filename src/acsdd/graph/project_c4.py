"""Classifying a change's architectural impact — the C4 projection.

NEW / MODIFIED / REMOVED / RELATED are **not stored on nodes**. They are a
function of a changeset, computed here, which is the difference between a
classification that can go stale and one that cannot. It also keeps
`GraphNode.status` free for the orthogonal durable lifecycle
(``proposed``/``active``/``deprecated``), which answers a different question.

This is exactly the closure the packaged `c4-component-diagram` skill computes
by hand today: *"include the direct dependencies of every NEW, MODIFIED and
REMOVED component, and nothing else."* That skill keeps owning the C4-PlantUML
macros and the colour standard — those come from outside this repository and do
not move when acsdd's detectors do, which is why it has no backing module of
its own. What it gains here is the classification table, so it no longer has to
derive one by inspection.
"""

from typing import Dict, List, Optional, Set

from acsdd.graph.changeset import ApplyOutcome
from acsdd.graph.model import Graph

# The four values the c4-component-diagram skill's AddElementTag lines use.
C4_STATUSES = ("new", "modified", "removed", "related")

# Which node types appear in a component diagram at all. Capabilities are
# excluded deliberately: a capability describes what an agent may *do*, and its
# dependency graph is not a component graph.
DIAGRAMMABLE = ("Component", "Container", "Interface", "DataEntity", "ExternalSystem")

# Edges that make a neighbour architecturally interesting. `implemented_by` is
# absent because it comes from a Capability, and `realized_in` because a module
# is below the diagram's altitude.
NEIGHBOUR_EDGES = ("depends_on", "exposes", "owns", "contains")


def classify(outcome: ApplyOutcome, before: Graph) -> Dict[str, str]:
    """``{node_id: one of C4_STATUSES}`` for everything the change touches.

    `before` is the graph as it stood; `outcome.graph` is how it would stand.
    Both are needed — a REMOVED node exists only in the former.

    An edge change counts as modifying **both** its endpoints. A component
    that gained a dependency changed, even though none of its own fields did,
    and a diagram that showed it unchanged would be lying about the thing the
    diagram is for.
    """
    after = outcome.graph
    classified: Dict[str, str] = {}

    def diagrammable(node_id: str) -> bool:
        node = after.nodes.get(node_id) or before.nodes.get(node_id)
        return node is not None and node.type in DIAGRAMMABLE

    for node_id in outcome.removed_nodes:
        if diagrammable(node_id):
            classified[node_id] = "removed"

    for node_id in outcome.added_nodes:
        if diagrammable(node_id):
            classified[node_id] = "new"

    for node_id in outcome.updated_nodes:
        if diagrammable(node_id) and node_id not in classified:
            classified[node_id] = "modified"

    # Endpoints of every added or removed edge, unless already accounted for.
    for edge_id in list(outcome.added_edges) + list(outcome.removed_edges):
        edge = after.edges.get(edge_id) or before.edges.get(edge_id)
        if edge is None:
            continue
        for endpoint in (edge.from_id, edge.to_id):
            if diagrammable(endpoint) and endpoint not in classified:
                classified[endpoint] = "modified"

    # Then the closure: one hop from anything above, and nothing else. The
    # objective is impact clarity, not repository visualization — a diagram of
    # everything communicates nothing.
    seeds: Set[str] = set(classified)
    for seed in sorted(seeds):
        for edge in after.out_edges(seed) + after.in_edges(seed):
            if edge.type not in NEIGHBOUR_EDGES:
                continue
            for endpoint in (edge.from_id, edge.to_id):
                if endpoint in classified or endpoint == seed:
                    continue
                if diagrammable(endpoint):
                    classified[endpoint] = "related"

    return dict(sorted(classified.items()))


def classification_report(outcome: ApplyOutcome, before: Graph,
                          requirements: Optional[List[str]] = None) -> Dict:
    """The `graph diff --for c4 --json` payload.

    Carries the node's name and type alongside its status so the consumer never
    has to look either up, and groups by status because that is how the diagram
    is drawn.
    """
    after = outcome.graph
    classified = classify(outcome, before)

    def describe(node_id: str) -> Dict:
        node = after.nodes.get(node_id) or before.nodes.get(node_id)
        evidence = [e.to_dict() for e in node.evidence] if node else []
        return {
            "id": node_id,
            "name": node.name if node else node_id,
            "type": node.type if node else "?",
            "technology": (node.attributes.get("technology") if node else None),
            "status": classified[node_id],
            "evidence": evidence,
        }

    by_status: Dict[str, List[Dict]] = {status: [] for status in C4_STATUSES}
    for node_id in classified:
        by_status[classified[node_id]].append(describe(node_id))

    return {
        "statuses": list(C4_STATUSES),
        "components": [describe(n) for n in classified],
        "by_status": by_status,
        "counts": {status: len(by_status[status]) for status in C4_STATUSES},
        "requirements": requirements or [],
    }
