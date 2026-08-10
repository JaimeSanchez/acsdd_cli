"""`apply_operations` — the pure half of `graph apply`.

Idempotency is the load-bearing property: an agent re-importing a revised PRD
re-emits its whole node set, and every operation whose effect is already
present has to land in `no_ops` rather than being counted as a change. If that
breaks, every second import produces a spurious revision.
"""

from acsdd.graph.changeset import GraphChangeSet, GraphOperation, apply_operations
from acsdd.graph.model import Graph, GraphEdge, GraphEvidence, GraphNode


def _changeset(*operations: GraphOperation, cid: str = "c") -> GraphChangeSet:
    return GraphChangeSet(id=cid, title="Title", operations=list(operations))


def _add(node: GraphNode) -> GraphOperation:
    return GraphOperation(op="add_node", target_id=node.id, node=node)


def _component(name: str, **kwargs) -> GraphNode:
    return GraphNode(id=f"cmp:{name}", type="Component", name=name.title(), **kwargs)


# -- purity --------------------------------------------------------------

def test_the_input_graph_is_never_mutated():
    """`graph apply --dry-run` is the real apply with the write left off, which
    only works if applying is free of side effects."""
    base = Graph(nodes={"cmp:a": _component("a")})

    outcome = apply_operations(base, _changeset(_add(_component("b"))))

    assert base.nodes.keys() == {"cmp:a"}
    assert outcome.graph.nodes.keys() == {"cmp:a", "cmp:b"}


# -- adds ----------------------------------------------------------------

def test_add_node_adds():
    outcome = apply_operations(Graph(), _changeset(_add(_component("a"))))

    assert outcome.added_nodes == ["cmp:a"]
    assert outcome.is_noop is False


def test_re_adding_an_identical_node_is_a_no_op():
    node = _component("a")
    outcome = apply_operations(Graph(nodes={node.id: node}), _changeset(_add(node)))

    assert outcome.added_nodes == []
    assert outcome.is_noop is True
    assert "already present and identical" in outcome.no_ops[0]


def test_re_adding_a_changed_node_counts_as_an_update():
    """A re-import re-emits its nodes wholesale. Refusing here would make every
    second import a diffing exercise for the agent."""
    before = _component("a")
    after = GraphNode(id="cmp:a", type="Component", name="A", summary="now described")
    outcome = apply_operations(Graph(nodes={before.id: before}), _changeset(_add(after)))

    assert outcome.updated_nodes == ["cmp:a"]
    assert outcome.added_nodes == []
    assert outcome.graph.nodes["cmp:a"].summary == "now described"


def test_add_edge_adds_and_re_adding_is_a_no_op():
    edge = GraphEdge.create("cmp:a", "depends_on", "cmp:b")
    op = GraphOperation(op="add_edge", target_id=edge.id, edge=edge)

    first = apply_operations(Graph(), _changeset(op))
    second = apply_operations(first.graph, _changeset(op))

    assert first.added_edges == ["cmp:a|depends_on|cmp:b"]
    assert second.added_edges == []
    assert second.is_noop is True


def test_an_edge_may_be_added_before_its_endpoints_exist():
    """Caught by the `dangling-edge` integrity rule against the final graph, so
    an agent never has to topologically sort its own output."""
    edge = GraphEdge.create("cmp:a", "depends_on", "cmp:b")
    outcome = apply_operations(Graph(), _changeset(
        GraphOperation(op="add_edge", target_id=edge.id, edge=edge),
        _add(_component("a"))))

    assert outcome.added_edges == ["cmp:a|depends_on|cmp:b"]
    assert outcome.refusals == []


def test_a_hand_built_edge_id_that_disagrees_is_replaced_and_reported():
    """Storing an id that lies about its own endpoints would make the whole
    derived-id scheme untrustworthy."""
    wrong = GraphEdge(id="whatever", type="depends_on", from_id="cmp:a", to_id="cmp:b")
    outcome = apply_operations(Graph(), _changeset(
        GraphOperation(op="add_edge", target_id=wrong.id, edge=wrong)))

    assert outcome.added_edges == ["cmp:a|depends_on|cmp:b"]
    assert "is not the derived" in outcome.refusals[0]


# -- updates -------------------------------------------------------------

def test_update_node_overwrites_only_the_named_fields():
    node = _component("a", summary="original", confidence=90)
    outcome = apply_operations(Graph(nodes={node.id: node}), _changeset(
        GraphOperation(op="update_node", target_id="cmp:a",
                       fields={"summary": "revised"})))

    updated = outcome.graph.nodes["cmp:a"]
    assert outcome.updated_nodes == ["cmp:a"]
    assert updated.summary == "revised"
    assert updated.confidence == 90
    assert updated.name == "A"


def test_update_node_can_set_structured_fields():
    node = _component("a")
    outcome = apply_operations(Graph(nodes={node.id: node}), _changeset(
        GraphOperation(op="update_node", target_id="cmp:a", fields={
            "evidence": [{"path": "src/A.php", "lines": "10"}],
            "attributes": {"technology": "symfony"}})))

    updated = outcome.graph.nodes["cmp:a"]
    assert updated.evidence == [GraphEvidence(path="src/A.php", lines="10")]
    assert updated.attributes == {"technology": "symfony"}


def test_update_node_on_a_missing_node_is_refused_not_raised():
    """One bad line in a large agent-authored changeset must yield a report,
    not a traceback."""
    outcome = apply_operations(Graph(), _changeset(
        GraphOperation(op="update_node", target_id="cmp:ghost",
                       fields={"summary": "x"})))

    assert outcome.refusals == ["update_node cmp:ghost: no such node"]
    assert outcome.is_noop is True


def test_update_node_refuses_to_change_id_or_type():
    """Changing either makes it a different node; the way to say that is
    remove plus add, where the integrity rules get a look at both."""
    node = _component("a")
    outcome = apply_operations(Graph(nodes={node.id: node}), _changeset(
        GraphOperation(op="update_node", target_id="cmp:a",
                       fields={"type": "Module", "id": "mod:a"})))

    assert outcome.graph.nodes["cmp:a"].type == "Component"
    assert "ignored unknown field(s) id, type" in outcome.refusals[0]


def test_an_update_that_changes_nothing_is_a_no_op():
    node = _component("a", summary="same")
    outcome = apply_operations(Graph(nodes={node.id: node}), _changeset(
        GraphOperation(op="update_node", target_id="cmp:a",
                       fields={"summary": "same"})))

    assert outcome.updated_nodes == []
    assert outcome.is_noop is True


# -- removals ------------------------------------------------------------

def test_remove_node_removes_it_and_its_edges():
    """Otherwise deleting a component would require the agent to enumerate
    every edge that touched it, and forgetting one is a dangling-edge error."""
    graph = Graph(nodes={"cmp:a": _component("a"), "cmp:b": _component("b")})
    for edge in (GraphEdge.create("cmp:a", "depends_on", "cmp:b"),
                 GraphEdge.create("cmp:b", "depends_on", "cmp:a")):
        graph.edges[edge.id] = edge

    outcome = apply_operations(graph, _changeset(
        GraphOperation(op="remove_node", target_id="cmp:a")))

    assert outcome.removed_nodes == ["cmp:a"]
    assert outcome.graph.edges == {}
    assert len(outcome.removed_edges) == 2


def test_removing_a_missing_node_is_a_no_op_not_a_refusal():
    outcome = apply_operations(Graph(), _changeset(
        GraphOperation(op="remove_node", target_id="cmp:ghost")))

    assert outcome.no_ops == ["remove_node cmp:ghost: not present"]
    assert outcome.refusals == []
    assert outcome.is_noop is True


def test_remove_edge_removes_only_that_edge():
    graph = Graph()
    for edge in (GraphEdge.create("cmp:a", "depends_on", "cmp:b"),
                 GraphEdge.create("cmp:a", "exposes", "ifc:b")):
        graph.edges[edge.id] = edge

    outcome = apply_operations(graph, _changeset(
        GraphOperation(op="remove_edge", target_id="cmp:a|depends_on|cmp:b")))

    assert outcome.removed_edges == ["cmp:a|depends_on|cmp:b"]
    assert outcome.graph.edges.keys() == {"cmp:a|exposes|ifc:b"}


# -- whole-changeset idempotency -----------------------------------------

def test_applying_a_changeset_twice_produces_no_second_change():
    """The property that stops a re-run cutting a spurious revision."""
    changeset = _changeset(
        _add(_component("a")),
        _add(_component("b")),
        GraphOperation(op="add_edge", target_id="cmp:a|depends_on|cmp:b",
                       edge=GraphEdge.create("cmp:a", "depends_on", "cmp:b")))

    first = apply_operations(Graph(), changeset)
    second = apply_operations(first.graph, changeset)

    assert first.is_noop is False
    assert second.is_noop is True
    assert second.graph.nodes.keys() == first.graph.nodes.keys()
    assert second.graph.edges.keys() == first.graph.edges.keys()


def test_operations_run_in_the_order_given():
    outcome = apply_operations(Graph(), _changeset(
        _add(_component("a")),
        GraphOperation(op="remove_node", target_id="cmp:a")))

    assert outcome.graph.nodes == {}
    assert outcome.added_nodes == ["cmp:a"]
    assert outcome.removed_nodes == ["cmp:a"]


def test_to_dict_reports_counts_and_the_noop_flag():
    outcome = apply_operations(Graph(), _changeset(_add(_component("a"))))
    payload = outcome.to_dict()

    assert payload["added_nodes"] == ["cmp:a"]
    assert payload["is_noop"] is False
    assert payload["refusals"] == []
