"""The `graph context --json` payload — the contract an agent writes against.

`test_the_payload_advertises_every_type_and_every_rule` is the guard here. A
rule an agent never learns about is a rule that fires as a surprise at apply
time, which is the whole experience this payload exists to prevent. It is the
direct analogue of
`test_discovery.py::test_review_covers_every_placeholder_real_discovery_emits`.
"""

import json
from datetime import date

import pytest

from acsdd.graph.context import (
    DEFAULT_MAX_NODES,
    PURPOSES,
    build_context,
    select_subgraph,
    vocabulary_payload,
)
from acsdd.graph.integrity import RULES
from acsdd.graph.model import Graph, GraphEdge, GraphNode
from acsdd.graph.vocabulary import EDGE_TYPES, NODE_TYPES

FIXED_DATE = date(2026, 8, 10)

MANIFESTS = {
    "DB-001": {"capability": {
        "id": "DB-001", "name": "Identify Doctrine Entities", "category": "DB",
        "description": "Introspects the mapping.\n",
        "quality_gates": ["test:unit-passing"]}},
    "DB-002": {"capability": {
        "id": "DB-002", "name": "Create Doctrine Entity", "category": "DB",
        "dependencies": [{"capability": "DB-001", "reason": "needs the inventory"}]}},
}


def _graph() -> Graph:
    graph = Graph(revision="0001-aaaaaaaa")
    for node in (
        GraphNode(id="cap:DB-001", type="Capability", name="Identify Doctrine Entities",
                  attributes={"category": "DB"}),
        GraphNode(id="cmp:entities", type="Component", name="Entities"),
        GraphNode(id="mod:entity", type="Module", name="Entity",
                  attributes={"path": "src/Entity"}),
        GraphNode(id="req:c.thing", type="Requirement", name="A thing"),
    ):
        graph.nodes[node.id] = node
    edge = GraphEdge.create("cap:DB-001", "implemented_by", "cmp:entities")
    graph.edges[edge.id] = edge
    return graph


def _payload(**kwargs) -> dict:
    defaults = dict(graph=_graph(), graph_path=".acsdd/graph/graph.json",
                    manifests=MANIFESTS, generated_at=FIXED_DATE)
    defaults.update(kwargs)
    graph = defaults.pop("graph")
    return build_context(graph, **defaults).to_dict()


# -- the guard -----------------------------------------------------------

def test_the_payload_advertises_every_type_and_every_rule():
    """A vocabulary entry or rule the payload omits is one the skill cannot
    know about, and the skill is forbidden from restating them itself."""
    data = _payload()

    assert {t["type"] for t in data["vocabulary"]["node_types"]} == set(NODE_TYPES)
    assert {e["type"] for e in data["vocabulary"]["edge_types"]} == set(EDGE_TYPES)
    assert {r["name"] for r in data["rules"]} == {r.name for r in RULES}


def test_the_payload_carries_the_whole_allowed_edge_matrix():
    """`pairs` IS the matrix. If it were partial, an agent following the
    payload would write edges that apply then refuses."""
    published = {e["type"]: {tuple(p) for p in e["pairs"]}
                 for e in vocabulary_payload()["edge_types"]}

    for name, spec in EDGE_TYPES.items():
        assert published[name] == set(spec.pairs), name


def test_every_rule_publishes_its_severity():
    """Severity is what tells an agent whether a finding blocks. A missing one
    turns the whole table into advice."""
    for rule in _payload()["rules"]:
        assert rule["severity"] in {"error", "warning", "advisory"}, rule
        assert rule["describes"], rule


def test_node_types_publish_their_required_attributes():
    by_type = {t["type"]: t for t in _payload()["vocabulary"]["node_types"]}

    assert by_type["Module"]["required_attributes"] == ["path"]
    assert by_type["Capability"]["required_attributes"] == ["category"]
    assert by_type["Requirement"]["durable"] is False
    assert by_type["Component"]["durable"] is True


# -- shape ---------------------------------------------------------------

def test_the_payload_has_a_stable_top_level_shape():
    assert set(_payload()) == {
        "purpose", "generated_at", "graph_path", "graph_revision", "change",
        "vocabulary", "rules", "changeset_format", "profile", "capabilities",
        "subgraph", "counts"}


def test_the_payload_is_json_serializable():
    """It is emitted through json.dumps; a dataclass or Path that slipped in
    would fail only at the CLI boundary."""
    json.dumps(_payload())


def test_counts_are_broken_down_by_type_and_layer():
    counts = _payload()["counts"]

    assert counts["nodes"] == 4
    assert counts["edges"] == 1
    assert counts["by_type"]["Capability"] == 1
    assert counts["by_layer"] == {"business": 1, "engineering": 2, "technical": 1}


# -- purpose-driven selection --------------------------------------------

def test_prd_import_carries_only_the_engineering_layer():
    """A PRD import needs to know what to attach requirements to, not the
    module tree."""
    subgraph = _payload(purpose="prd-import")["subgraph"]

    assert {n["id"] for n in subgraph["nodes"]} == {"cap:DB-001", "cmp:entities"}
    assert subgraph["selected_by"] == "layer:engineering"


def test_repo_map_adds_the_technical_layer():
    subgraph = _payload(purpose="repo-map")["subgraph"]

    assert "mod:entity" in {n["id"] for n in subgraph["nodes"]}
    assert "req:c.thing" not in {n["id"] for n in subgraph["nodes"]}


def test_spec_check_carries_everything():
    subgraph = _payload(purpose="spec-check")["subgraph"]

    assert len(subgraph["nodes"]) == 4


@pytest.mark.parametrize("purpose", sorted(PURPOSES))
def test_every_purpose_names_real_layers(purpose):
    from acsdd.graph.vocabulary import LAYERS

    assert set(PURPOSES[purpose]) <= set(LAYERS)


def test_only_edges_between_kept_nodes_are_carried():
    """An edge to a node that was filtered out is a dangling edge in the
    payload, which is exactly the shape the agent is told never to write."""
    subgraph = _payload(purpose="prd-import")["subgraph"]
    kept = {n["id"] for n in subgraph["nodes"]}

    for edge in subgraph["edges"]:
        assert edge["from"] in kept and edge["to"] in kept


# -- truncation ----------------------------------------------------------

def test_truncation_is_bounded_and_says_so():
    """An unbounded payload is an unbounded prompt."""
    graph = Graph()
    for i in range(50):
        graph.nodes[f"cmp:c{i:03d}"] = GraphNode(
            id=f"cmp:c{i:03d}", type="Component", name=f"C{i}")

    subgraph = select_subgraph(graph, ("engineering",), max_nodes=10)

    assert subgraph["node_count"] == 10
    assert subgraph["truncated"] is True
    assert subgraph["max_nodes"] == 10


def test_truncation_keeps_the_earlier_layers_first():
    """By layer priority, never arbitrarily: dropping happens from the least
    relevant end so a short answer is still the right short answer."""
    graph = Graph()
    graph.nodes["cmp:a"] = GraphNode(id="cmp:a", type="Component", name="A")
    for i in range(5):
        graph.nodes[f"mod:m{i}"] = GraphNode(
            id=f"mod:m{i}", type="Module", name=f"M{i}", attributes={"path": "src"})

    subgraph = select_subgraph(graph, ("engineering", "technical"), max_nodes=2)

    assert subgraph["nodes"][0]["id"] == "cmp:a"
    assert subgraph["truncated"] is True


def test_an_untruncated_subgraph_says_so():
    assert _payload()["subgraph"]["truncated"] is False
    assert _payload()["subgraph"]["max_nodes"] == DEFAULT_MAX_NODES


# -- profile -------------------------------------------------------------

def test_a_finalized_profile_is_usable():
    profile = {"meta": {"id": "acme", "status": "active"},
               "technology_stack": {"language": "php", "framework": "symfony"}}

    payload = _payload(profile=profile, profile_path=".acsdd/profiles/acme.yaml")["profile"]

    assert payload["usable"] is True
    assert payload["unresolved_field_count"] == 0
    assert payload["technology_stack"]["framework"] == "symfony"


def test_a_draft_profile_is_not_usable():
    """Mirrors what capability-plan already says out loud: recommendations
    derived from [REVIEW REQUIRED] placeholders are derived from nothing."""
    profile = {"meta": {"id": "acme", "status": "draft"},
               "technology_stack": {"language": "php",
                                    "orm": "[REVIEW REQUIRED — infer from dependencies]"}}

    payload = _payload(profile=profile)["profile"]

    assert payload["usable"] is False
    assert payload["unresolved_field_count"] == 1


def test_an_active_profile_with_placeholders_is_still_not_usable():
    profile = {"meta": {"id": "acme", "status": "active"},
               "technology_stack": {"orm": "[REVIEW REQUIRED]"}}

    assert _payload(profile=profile)["profile"]["usable"] is False


def test_no_profile_is_null_rather_than_an_error():
    """A repo can import a PRD before it has a profile."""
    assert _payload()["profile"] is None


# -- capabilities --------------------------------------------------------

def test_capabilities_report_whether_their_node_exists():
    """`in_graph` is what tells the agent to create the node or merely
    reference it."""
    capabilities = {c["id"]: c for c in _payload()["capabilities"]}

    assert capabilities["DB-001"]["in_graph"] is True
    assert capabilities["DB-002"]["in_graph"] is False
    assert capabilities["DB-001"]["node_id"] == "cap:DB-001"


def test_capability_dependencies_and_gates_are_carried():
    capabilities = {c["id"]: c for c in _payload()["capabilities"]}

    assert capabilities["DB-002"]["dependencies"] == ["DB-001"]
    assert capabilities["DB-001"]["quality_gates"] == ["test:unit-passing"]
    assert capabilities["DB-001"]["description"] == "Introspects the mapping."


def test_no_manifests_gives_an_empty_list_not_an_error():
    assert _payload(manifests={})["capabilities"] == []


# -- changeset format ----------------------------------------------------

def test_the_changeset_format_carries_runnable_commands():
    change = {"id": "checkout-guest",
              "changeset_path": ".acsdd/changes/checkout-guest/changeset.json"}

    fmt = _payload(change=change)["changeset_format"]

    assert fmt["envelope"] == "changeset"
    assert fmt["write_to"] == ".acsdd/changes/checkout-guest/changeset.json"
    assert fmt["dry_run_command"].startswith("acsdd graph apply ")
    assert "--dry-run" in fmt["dry_run_command"]
    assert "--change checkout-guest" in fmt["validate_command"]


def test_the_changeset_format_states_the_id_and_edge_rules():
    """Both are integrity errors if broken, so the agent has to learn them
    before it writes rather than from a refusal."""
    fmt = _payload(change={"id": "checkout-guest"})["changeset_format"]

    assert "checkout-guest." in fmt["business_id_rule"]
    assert "do not" in fmt["edge_id_rule"]
    assert "update_edge" in fmt["no_update_edge"]


def test_the_example_uses_the_real_change_id_and_base_revision():
    fmt = _payload(change={"id": "checkout-guest"})["changeset_format"]
    changeset = fmt["example"]["changeset"]

    assert changeset["id"] == "checkout-guest"
    assert changeset["base_revision"] == "0001-aaaaaaaa"
    assert changeset["operations"][0]["node"]["id"].startswith("req:checkout-guest.")


def test_the_example_validates_against_the_changeset_schema():
    """The worked example is what an agent copies and edits down. One that
    fails the schema as written teaches the wrong shape, so it is a real
    document rather than a bracket-filled skeleton."""
    from acsdd.graph.validator import validate_changeset_document

    example = _payload(change={"id": "checkout-guest"})["changeset_format"]["example"]

    result = validate_changeset_document(example)
    assert result.ok, result.errors


def test_the_example_also_validates_without_a_change_in_context():
    from acsdd.graph.validator import validate_changeset_document

    example = _payload()["changeset_format"]["example"]

    result = validate_changeset_document(example)
    assert result.ok, result.errors


def test_the_example_would_survive_the_integrity_rules_it_can_reach():
    """Nodes and edges the example demonstrates must not themselves be the
    shape the rules reject — a legal edge pair, a scoped business id."""
    from acsdd.graph.vocabulary import edge_pair_allowed

    example = _payload(change={"id": "checkout-guest"})["changeset_format"]["example"]
    operations = example["changeset"]["operations"]

    assert operations[0]["node"]["id"].startswith("req:checkout-guest.")
    assert edge_pair_allowed("Requirement", operations[1]["edge"]["type"], "Capability")


# -- change --------------------------------------------------------------

def test_no_change_is_null_and_the_format_still_explains_itself():
    data = _payload()

    assert data["change"] is None
    assert data["changeset_format"]["business_id_rule"]
    assert data["changeset_format"]["write_to"].endswith("changeset.json")
