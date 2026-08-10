"""The C4 impact projection.

The four statuses are computed from a changeset, never stored on a node. These
tests pin that: the same graph yields different classifications for different
changesets, which is the property that makes the projection unable to go stale.
"""

from acsdd.graph.changeset import GraphChangeSet, GraphOperation, apply_operations
from acsdd.graph.model import Graph, GraphEdge, GraphEvidence, GraphNode
from acsdd.graph.project_c4 import (
    C4_STATUSES,
    classification_report,
    classify,
)


def _component(slug: str, **kwargs) -> GraphNode:
    return GraphNode(id=f"cmp:{slug}", type="Component", name=slug.title(), **kwargs)


def _base() -> Graph:
    """payment -> ledger -> audit, plus an unrelated component."""
    graph = Graph(nodes={n.id: n for n in (
        _component("payment", attributes={"technology": "symfony"}),
        _component("ledger"),
        _component("audit"),
        _component("unrelated"),
    )})
    for tail, head in (("payment", "ledger"), ("ledger", "audit")):
        edge = GraphEdge.create(f"cmp:{tail}", "depends_on", f"cmp:{head}")
        graph.edges[edge.id] = edge
    return graph


def _apply(graph: Graph, *operations: GraphOperation):
    changeset = GraphChangeSet(id="c", title="T", operations=list(operations))
    return apply_operations(graph, changeset)


# -- the four statuses ---------------------------------------------------

def test_an_added_component_is_new():
    outcome = _apply(_base(), GraphOperation(
        op="add_node", target_id="cmp:retry", node=_component("retry")))

    assert classify(outcome, _base())["cmp:retry"] == "new"


def test_a_removed_component_is_removed():
    """It exists only in the *before* graph, which is why classify needs both."""
    base = _base()
    outcome = _apply(base, GraphOperation(op="remove_node", target_id="cmp:unrelated"))

    assert classify(outcome, base)["cmp:unrelated"] == "removed"


def test_an_updated_component_is_modified():
    base = _base()
    outcome = _apply(base, GraphOperation(
        op="update_node", target_id="cmp:payment", fields={"summary": "revised"}))

    assert classify(outcome, base)["cmp:payment"] == "modified"


def test_gaining_an_edge_modifies_both_endpoints():
    """A component that gained a dependency changed, even though none of its
    own fields did. A diagram showing it unchanged would be lying about the
    thing the diagram is for."""
    base = _base()
    edge = GraphEdge.create("cmp:payment", "depends_on", "cmp:unrelated")
    outcome = _apply(base, GraphOperation(op="add_edge", target_id=edge.id, edge=edge))

    classified = classify(outcome, base)
    assert classified["cmp:payment"] == "modified"
    assert classified["cmp:unrelated"] == "modified"


def test_a_neighbour_one_hop_away_is_related():
    base = _base()
    new = _component("retry")
    edge = GraphEdge.create("cmp:retry", "depends_on", "cmp:payment")
    outcome = _apply(base,
                     GraphOperation(op="add_node", target_id=new.id, node=new),
                     GraphOperation(op="add_edge", target_id=edge.id, edge=edge))

    classified = classify(outcome, base)
    assert classified["cmp:retry"] == "new"
    assert classified["cmp:payment"] == "modified"     # gained an incoming edge
    assert classified["cmp:ledger"] == "related"       # one hop from payment


def test_the_closure_stops_at_one_hop():
    """The objective is impact clarity, not repository visualization — a
    diagram of everything communicates nothing."""
    base = _base()
    new = _component("retry")
    edge = GraphEdge.create("cmp:retry", "depends_on", "cmp:payment")
    outcome = _apply(base,
                     GraphOperation(op="add_node", target_id=new.id, node=new),
                     GraphOperation(op="add_edge", target_id=edge.id, edge=edge))

    classified = classify(outcome, base)
    assert "cmp:audit" not in classified, "two hops away must not be drawn"


def test_a_stronger_status_is_never_downgraded_to_related():
    base = _base()
    new = _component("ledger2")
    edge = GraphEdge.create("cmp:ledger2", "depends_on", "cmp:ledger")
    outcome = _apply(base,
                     GraphOperation(op="add_node", target_id=new.id, node=new),
                     GraphOperation(op="add_edge", target_id=edge.id, edge=edge))

    assert classify(outcome, base)["cmp:ledger2"] == "new"


# -- what is diagrammable ------------------------------------------------

def test_capabilities_never_appear_in_a_component_diagram():
    """A capability describes what an agent may *do*; its dependency graph is
    not a component graph."""
    base = _base()
    capability = GraphNode(id="cap:BE-001", type="Capability", name="Endpoint",
                           attributes={"category": "BE"})
    outcome = _apply(base, GraphOperation(op="add_node", target_id=capability.id,
                                          node=capability))

    assert "cap:BE-001" not in classify(outcome, base)


def test_requirements_and_modules_are_not_diagrammable():
    base = _base()
    for node in (GraphNode(id="req:c.thing", type="Requirement", name="A thing"),
                 GraphNode(id="mod:src", type="Module", name="Src",
                           attributes={"path": "src"})):
        base_outcome = _apply(base, GraphOperation(op="add_node", target_id=node.id,
                                                   node=node))
        assert node.id not in classify(base_outcome, base), node.id


def test_interfaces_and_data_entities_are_diagrammable():
    base = _base()
    for node in (GraphNode(id="ifc:pay-api", type="Interface", name="Pay API"),
                 GraphNode(id="ent:invoice", type="DataEntity", name="Invoice")):
        outcome = _apply(base, GraphOperation(op="add_node", target_id=node.id,
                                              node=node))
        assert classify(outcome, base)[node.id] == "new", node.id


def test_a_module_edge_does_not_pull_a_component_into_the_diagram():
    """`realized_in` is below the diagram's altitude — following it would drag
    the whole file tree in."""
    base = _base()
    module = GraphNode(id="mod:payment", type="Module", name="Payment",
                       attributes={"path": "src/Payment"})
    edge = GraphEdge.create("cmp:payment", "realized_in", "mod:payment")
    outcome = _apply(base,
                     GraphOperation(op="add_node", target_id=module.id, node=module),
                     GraphOperation(op="add_edge", target_id=edge.id, edge=edge))

    classified = classify(outcome, base)
    assert classified["cmp:payment"] == "modified"
    assert "mod:payment" not in classified


# -- the report ----------------------------------------------------------

def test_the_report_groups_by_status_and_counts():
    base = _base()
    new = _component("retry", evidence=[GraphEvidence(path="src/Retry.php", lines="10")])
    edge = GraphEdge.create("cmp:retry", "depends_on", "cmp:payment")
    outcome = _apply(base,
                     GraphOperation(op="add_node", target_id=new.id, node=new),
                     GraphOperation(op="add_edge", target_id=edge.id, edge=edge))

    report = classification_report(outcome, base)

    assert report["statuses"] == list(C4_STATUSES)
    assert [c["id"] for c in report["by_status"]["new"]] == ["cmp:retry"]
    assert report["counts"]["new"] == 1
    assert sum(report["counts"].values()) == len(report["components"])


def test_the_report_carries_name_type_technology_and_evidence():
    """So the consumer never has to look any of them up."""
    base = _base()
    outcome = _apply(base, GraphOperation(
        op="update_node", target_id="cmp:payment", fields={"summary": "revised"}))

    entry = classification_report(outcome, base)["by_status"]["modified"][0]

    assert entry["name"] == "Payment"
    assert entry["type"] == "Component"
    assert entry["technology"] == "symfony"
    assert entry["evidence"] == []


def test_the_report_lists_the_requirements_it_was_given():
    base = _base()
    outcome = _apply(base, GraphOperation(op="add_node", target_id="cmp:retry",
                                          node=_component("retry")))

    report = classification_report(outcome, base, requirements=["req:c.retry-failed"])

    assert report["requirements"] == ["req:c.retry-failed"]


def test_a_changeset_touching_nothing_diagrammable_reports_empty():
    base = _base()
    node = GraphNode(id="req:c.thing", type="Requirement", name="A thing")
    outcome = _apply(base, GraphOperation(op="add_node", target_id=node.id, node=node))

    report = classification_report(outcome, base)

    assert report["components"] == []
    assert all(count == 0 for count in report["counts"].values())


def test_the_same_graph_classifies_differently_for_a_different_changeset():
    """The point of computing rather than storing: nothing about cmp:payment
    changed between these two runs, only what is being proposed."""
    base = _base()

    touching = _apply(base, GraphOperation(op="update_node", target_id="cmp:payment",
                                            fields={"summary": "revised"}))
    elsewhere = _apply(base, GraphOperation(op="update_node", target_id="cmp:unrelated",
                                             fields={"summary": "revised"}))

    assert classify(touching, base)["cmp:payment"] == "modified"
    assert "cmp:payment" not in classify(elsewhere, base)
