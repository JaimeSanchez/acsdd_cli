"""Round-trip and determinism tests for the graph domain model.

graph.json is a checked-in file that humans review in diffs, so byte stability
is a correctness property here, not a nicety. `test_serialization_is_byte_stable`
and `test_reserializing_a_parsed_graph_is_a_noop` are what make it one.
"""

import json
from datetime import date

import pytest

from acsdd.graph.model import (
    Graph,
    GraphEdge,
    GraphEvidence,
    GraphLoadError,
    GraphNode,
    GraphSource,
    edge_id,
    graph_hash,
    serialize,
)
from acsdd.graph.validator import validate_graph_document

FIXED_DATE = date(2026, 8, 10)


def _sample_graph() -> Graph:
    source = GraphSource(kind="agent", ref="docs/prd/checkout.md", at="2026-08-10",
                         agent="claude-code/graph-import", acsdd_version="0.10.0")
    graph = Graph()
    for node in (
        GraphNode(id="cap:BE-001", type="Capability", name="Create Symfony Endpoint",
                  attributes={"category": "BE"}, source=source),
        GraphNode(id="cmp:checkout", type="Component", name="Checkout Controller",
                  summary="Owns the HTTP surface for checkout.",
                  attributes={"technology": "symfony"},
                  evidence=[GraphEvidence(path="src/Controller/Checkout.php", lines="12-40"),
                            GraphEvidence(path="src/Controller/Checkout.php", lines="1-11")],
                  source=source, confidence=80),
        GraphNode(id="mod:controller", type="Module", name="Controller",
                  attributes={"path": "src/Controller"}, source=source),
    ):
        graph.nodes[node.id] = node

    for edge in (
        GraphEdge.create("cap:BE-001", "implemented_by", "cmp:checkout",
                         rationale="The endpoint lands here.", source=source),
        GraphEdge.create("cmp:checkout", "realized_in", "mod:controller", source=source),
    ):
        graph.edges[edge.id] = edge

    return graph


# -- ids -----------------------------------------------------------------

def test_edge_id_is_derived_from_the_triple():
    assert edge_id("cap:BE-001", "implemented_by", "cmp:checkout") == \
        "cap:BE-001|implemented_by|cmp:checkout"


def test_edge_create_derives_its_own_id():
    edge = GraphEdge.create("cmp:a", "depends_on", "cmp:b")
    assert edge.id == edge_id("cmp:a", "depends_on", "cmp:b")


def test_re_adding_the_same_edge_collapses_onto_one_key():
    """Derived ids are what make `graph apply` idempotent without a dedup pass.
    If this ever fails, re-running an import starts accumulating duplicates."""
    graph = Graph()
    for rationale in ("first pass", "second pass"):
        edge = GraphEdge.create("cmp:a", "depends_on", "cmp:b", rationale=rationale)
        graph.edges[edge.id] = edge
    assert len(graph.edges) == 1


# -- layer derivation ----------------------------------------------------

def test_layer_is_derived_from_type():
    assert GraphNode(id="req:x.y", type="Requirement", name="Pay as guest").layer == "business"
    assert GraphNode(id="cmp:x", type="Component", name="Checkout").layer == "engineering"
    assert GraphNode(id="mod:x", type="Module", name="Controller").layer == "technical"


def test_layer_raises_rather_than_guessing_for_an_unknown_type():
    """Silently answering 'business' would let an unknown type slip past every
    layer-crossing check. The `unknown-type` integrity finding is the intended
    way to hear about this."""
    with pytest.raises(KeyError):
        _ = GraphNode(id="xx:y", type="Invented", name="Nope").layer


# -- round trip ----------------------------------------------------------

def test_to_dict_from_dict_round_trip_preserves_everything():
    """Identity holds from the canonical form, which is the form on disk.

    The first pass is not an identity, deliberately: `to_dict` sorts evidence,
    so a graph built with evidence in authoring order comes back in canonical
    order. That is the point of canonicalizing. What has to hold — and what
    every later read depends on — is that the canonical form is a fixpoint.
    """
    once = Graph.from_dict(_sample_graph().to_dict(generated_at=FIXED_DATE))
    twice = Graph.from_dict(once.to_dict(generated_at=FIXED_DATE))

    assert twice.nodes == once.nodes
    assert twice.edges == once.edges


def test_the_first_serialization_canonicalizes_evidence_order():
    original = _sample_graph()
    restored = Graph.from_dict(original.to_dict(generated_at=FIXED_DATE))

    authored = [(e.path, e.lines) for e in original.nodes["cmp:checkout"].evidence]
    canonical = [(e.path, e.lines) for e in restored.nodes["cmp:checkout"].evidence]
    assert authored == [("src/Controller/Checkout.php", "12-40"),
                        ("src/Controller/Checkout.php", "1-11")]
    assert canonical == sorted(authored)


def test_defaults_are_omitted_from_the_document_and_restored_on_parse():
    """Empty collections and defaulted fields are omitted so a 3000-line file
    stays scannable; from_dict supplies them back. A record that round-trips
    but grows keys each time would make every re-serialize a diff."""
    node = GraphNode(id="cmp:bare", type="Component", name="Bare")
    payload = node.to_dict()

    assert payload == {"id": "cmp:bare", "type": "Component", "name": "Bare"}
    assert GraphNode.from_dict(payload) == node


def test_non_default_confidence_and_status_survive():
    node = GraphNode(id="cmp:x", type="Component", name="X",
                     confidence=55, status="proposed")
    assert node.to_dict()["confidence"] == 55
    assert node.to_dict()["status"] == "proposed"
    assert GraphNode.from_dict(node.to_dict()) == node


def test_duplicate_node_id_raises_rather_than_being_swallowed():
    """dict-keying would drop the second record silently, making the very bug
    the `id-unique` rule exists to catch un-catchable."""
    doc = {"graph": {"meta": {"version": "1.0.0", "generated_at": "2026-08-10"},
                     "nodes": [{"id": "cmp:x", "type": "Component", "name": "One"},
                               {"id": "cmp:x", "type": "Component", "name": "Two"}],
                     "edges": []}}
    with pytest.raises(GraphLoadError, match="duplicate node id 'cmp:x'"):
        Graph.from_dict(doc)


def test_duplicate_edge_id_raises():
    doc = {"graph": {"meta": {"version": "1.0.0", "generated_at": "2026-08-10"},
                     "nodes": [],
                     "edges": [{"id": "a|depends_on|b", "type": "depends_on",
                                "from": "cmp:a", "to": "cmp:b"},
                               {"id": "a|depends_on|b", "type": "depends_on",
                                "from": "cmp:a", "to": "cmp:b"}]}}
    with pytest.raises(GraphLoadError, match="duplicate edge id"):
        Graph.from_dict(doc)


def test_a_document_without_a_graph_envelope_raises():
    with pytest.raises(GraphLoadError, match="no top-level 'graph' mapping"):
        Graph.from_dict({"nodes": [], "edges": []})


def test_an_unreadable_node_raises_rather_than_being_skipped():
    doc = {"graph": {"meta": {"version": "1.0.0", "generated_at": "2026-08-10"},
                     "nodes": [{"id": "cmp:x", "type": "Component"}], "edges": []}}
    with pytest.raises(GraphLoadError, match="unreadable node"):
        Graph.from_dict(doc)


def test_an_edge_without_an_id_gets_the_derived_one():
    """A changeset may omit the edge id — the schema makes it optional there —
    so parsing has to supply it rather than failing."""
    doc = {"graph": {"meta": {"version": "1.0.0", "generated_at": "2026-08-10"},
                     "nodes": [],
                     "edges": [{"type": "depends_on", "from": "cmp:a", "to": "cmp:b"}]}}
    graph = Graph.from_dict(doc)
    assert list(graph.edges) == ["cmp:a|depends_on|cmp:b"]


# -- determinism ---------------------------------------------------------

def test_serialization_is_byte_stable():
    graph = _sample_graph()
    assert serialize(graph, generated_at=FIXED_DATE) == serialize(graph, generated_at=FIXED_DATE)


def test_reserializing_a_parsed_graph_is_a_noop():
    """The property `catalog verify` relies on for CATALOG.md, applied to
    graph.json: reading a file and writing it back must produce no diff."""
    text = serialize(_sample_graph(), generated_at=FIXED_DATE)
    reparsed = Graph.from_dict(json.loads(text))
    assert serialize(reparsed, generated_at=FIXED_DATE) == text


def test_nodes_and_edges_are_sorted_arrays_not_id_keyed_objects():
    """An id-keyed object prints every id twice and neither diff nor merge
    tools respect JSON object ordering."""
    body = _sample_graph().to_dict(generated_at=FIXED_DATE)["graph"]

    assert isinstance(body["nodes"], list) and isinstance(body["edges"], list)
    assert [n["id"] for n in body["nodes"]] == sorted(n["id"] for n in body["nodes"])
    assert [e["id"] for e in body["edges"]] == sorted(e["id"] for e in body["edges"])


def test_attributes_and_evidence_are_sorted_within_a_record():
    node = GraphNode(
        id="cmp:x", type="Component", name="X",
        attributes={"technology": "symfony", "owner": "team"},
        evidence=[GraphEvidence(path="b.php", lines="2"),
                  GraphEvidence(path="a.php", lines="9"),
                  GraphEvidence(path="a.php", lines="1")])
    payload = node.to_dict()

    assert list(payload["attributes"]) == ["owner", "technology"]
    assert [(e["path"], e["lines"]) for e in payload["evidence"]] == [
        ("a.php", "1"), ("a.php", "9"), ("b.php", "2")]


def test_generated_at_is_injectable_and_a_date():
    body = _sample_graph().to_dict(generated_at=FIXED_DATE)["graph"]
    assert body["meta"]["generated_at"] == "2026-08-10"


def test_graph_hash_ignores_meta_but_tracks_content():
    """Hashing meta would make the digest self-referential and would change it
    every day even when nothing moved."""
    graph = _sample_graph()
    before = graph_hash(graph)

    graph.revision = "0009-deadbeef"
    assert graph_hash(graph) == before

    graph.nodes["cmp:new"] = GraphNode(id="cmp:new", type="Component", name="New")
    assert graph_hash(graph) != before


def test_serialize_ends_with_exactly_one_newline():
    text = serialize(_sample_graph(), generated_at=FIXED_DATE)
    assert text.endswith("}\n") and not text.endswith("\n\n")


# -- traversal -----------------------------------------------------------

def test_out_and_in_edges_filter_by_type_and_sort():
    graph = _sample_graph()
    assert [e.id for e in graph.out_edges("cap:BE-001")] == \
        ["cap:BE-001|implemented_by|cmp:checkout"]
    assert graph.out_edges("cap:BE-001", "depends_on") == []
    assert [e.id for e in graph.in_edges("cmp:checkout")] == \
        ["cap:BE-001|implemented_by|cmp:checkout"]


def test_neighbours_are_undirected_deduplicated_and_sorted():
    graph = _sample_graph()
    assert graph.neighbours("cmp:checkout") == ["cap:BE-001", "mod:controller"]


def test_merged_with_lets_the_overlay_win():
    """A change overlay is the thing being proposed, so it wins on collision."""
    base = _sample_graph()
    overlay = Graph(nodes={"cmp:checkout": GraphNode(
        id="cmp:checkout", type="Component", name="Checkout Controller (revised)")})

    merged = base.merged_with(overlay)
    assert merged.nodes["cmp:checkout"].name == "Checkout Controller (revised)"
    assert base.nodes["cmp:checkout"].name == "Checkout Controller", "base was mutated"
    assert merged.revision == base.revision


# -- the schema agrees with what we emit ---------------------------------

def test_a_serialized_graph_validates_against_the_schema():
    """The model and the schema are written by hand in two places; this is what
    catches an emitted field the schema forbids."""
    doc = json.loads(serialize(_sample_graph(), generated_at=FIXED_DATE,
                               acsdd_version="0.10.0"))
    result = validate_graph_document(doc)
    assert result.ok, result.errors


def test_an_empty_graph_validates():
    doc = json.loads(serialize(Graph(), generated_at=FIXED_DATE))
    result = validate_graph_document(doc)
    assert result.ok, result.errors
