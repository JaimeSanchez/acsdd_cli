"""One test per integrity rule, plus the table-level guards.

The rules are the whole reason a graph assembled by an agent can be trusted, so
each one gets a test that makes it fire and a check that it lands in the right
list. Severity is which list a finding lands in; a rule that quietly moves from
`errors` to `advisories` stops blocking `graph apply` and nothing else would
notice.
"""

import pytest

from acsdd.graph.integrity import (
    RULES,
    SEVERITIES,
    IntegrityReport,
    RuleContext,
    check_integrity,
    find_cycle,
)
from acsdd.graph.model import Graph, GraphEdge, GraphEvidence, GraphNode, GraphSource
from acsdd.graph.vocabulary import LOW_CONFIDENCE

AGENT = GraphSource(kind="agent", ref="docs/prd/x.md", at="2026-08-10")
CITED = [GraphEvidence(path="src/X.php", lines="10")]


def _node(node_id: str, node_type: str, **kwargs) -> GraphNode:
    kwargs.setdefault("name", node_id.split(":", 1)[-1].title())
    return GraphNode(id=node_id, type=node_type, **kwargs)


def _graph(*nodes: GraphNode, edges=()) -> Graph:
    graph = Graph(nodes={n.id: n for n in nodes})
    for edge in edges:
        graph.edges[edge.id] = edge
    return graph


def _rules_fired(report: IntegrityReport) -> set:
    return {f.rule for f in report.errors + report.warnings + report.advisories}


def _subjects(report: IntegrityReport, rule: str) -> list:
    return sorted(f.subject for f in report.errors + report.warnings + report.advisories
                  if f.rule == rule)


# A graph that trips nothing, used as the baseline every test perturbs.
def _clean_graph() -> Graph:
    return _graph(
        _node("cap:BE-001", "Capability", attributes={"category": "BE"}),
        _node("cmp:checkout", "Component"),
        _node("mod:controller", "Module", attributes={"path": "src/Controller"}),
        edges=[
            GraphEdge.create("cap:BE-001", "implemented_by", "cmp:checkout"),
            GraphEdge.create("cmp:checkout", "realized_in", "mod:controller"),
        ])


def test_the_baseline_graph_is_clean():
    """Every test below perturbs this graph, so it has to start silent — a
    baseline that already fires makes each assertion meaningless."""
    report = check_integrity(_clean_graph())

    assert report.errors == []
    assert report.warnings == []
    assert report.advisories == []
    assert report.ok and report.clean


# -- errors --------------------------------------------------------------

def test_unknown_node_type_is_an_error():
    report = check_integrity(_graph(_node("xx:thing", "Invented")))

    assert "unknown-type" in {f.rule for f in report.errors}
    assert _subjects(report, "unknown-type") == ["xx:thing"]


def test_unknown_edge_type_is_an_error():
    graph = _graph(_node("cmp:a", "Component"), _node("cmp:b", "Component"))
    edge = GraphEdge.create("cmp:a", "invented", "cmp:b")
    graph.edges[edge.id] = edge

    report = check_integrity(graph)

    assert "unknown-type" in {f.rule for f in report.errors}


def test_an_id_prefix_that_disagrees_with_the_type_is_an_error():
    """The prefix is what lets a reviewer read an edge in a raw diff without
    looking the node up."""
    report = check_integrity(_graph(_node("mod:thing", "Component")))

    finding = next(f for f in report.errors if f.rule == "id-prefix-matches-type")
    assert "names Module" in finding.message
    assert "cmp:thing" in finding.fix


def test_a_missing_required_attribute_is_an_error():
    report = check_integrity(_graph(_node("mod:x", "Module")))

    finding = next(f for f in report.errors if f.rule == "required-attribute")
    assert "requires an 'path' attribute" in finding.message


def test_a_dangling_edge_is_an_error():
    graph = _graph(_node("cmp:a", "Component"))
    edge = GraphEdge.create("cmp:a", "depends_on", "cmp:ghost")
    graph.edges[edge.id] = edge

    report = check_integrity(graph)

    finding = next(f for f in report.errors if f.rule == "dangling-edge")
    assert "cmp:ghost" in finding.message


def test_an_edge_pair_outside_the_matrix_is_an_error():
    """The layer-crossing rule. A Requirement may not name a Module — a PRD
    does not name a directory."""
    graph = _graph(
        _node("req:c.pay", "Requirement"),
        _node("mod:x", "Module", attributes={"path": "src"}))
    edge = GraphEdge.create("req:c.pay", "realized_in", "mod:x")
    graph.edges[edge.id] = edge

    report = check_integrity(graph)

    finding = next(f for f in report.errors if f.rule == "illegal-edge-pair")
    assert "business -> technical" in finding.message
    assert "Component -> Module" in finding.fix


def test_a_cycle_on_an_acyclic_edge_type_is_an_error():
    graph = _graph(_node("cmp:a", "Component"), _node("cmp:b", "Component"),
                   _node("cmp:c", "Component"))
    for pair in (("cmp:a", "cmp:b"), ("cmp:b", "cmp:c"), ("cmp:c", "cmp:a")):
        edge = GraphEdge.create(pair[0], "depends_on", pair[1])
        graph.edges[edge.id] = edge

    report = check_integrity(graph)

    cycles = [f for f in report.errors if f.rule == "cycle"]
    assert len(cycles) == 1, "one loop must be reported once, not once per member"
    assert "circular 'depends_on'" in cycles[0].message


def test_a_self_loop_on_an_acyclic_edge_type_is_a_cycle():
    graph = _graph(_node("cmp:a", "Component"))
    edge = GraphEdge.create("cmp:a", "depends_on", "cmp:a")
    graph.edges[edge.id] = edge

    report = check_integrity(graph)

    assert "cycle" in {f.rule for f in report.errors}


def test_a_diamond_on_an_acyclic_edge_type_is_not_a_cycle():
    """Two paths to the same node is a normal shape. Reporting it would make
    the rule fire on most real dependency graphs and stop being read."""
    graph = _graph(*(_node(f"cmp:{n}", "Component") for n in "abcd"))
    for tail, head in (("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")):
        edge = GraphEdge.create(f"cmp:{tail}", "depends_on", f"cmp:{head}")
        graph.edges[edge.id] = edge

    assert not [f for f in check_integrity(graph).errors if f.rule == "cycle"]


def test_a_requirement_with_no_capability_is_an_error():
    graph = _clean_graph()
    graph.nodes["req:c.pay"] = _node("req:c.pay", "Requirement", name="Pay as guest")

    report = check_integrity(graph, RuleContext(change_id="c"))

    finding = next(f for f in report.errors
                   if f.rule == "requirement-without-capability")
    assert "capability recommend" in finding.fix


def test_a_requirement_with_a_capability_is_fine():
    graph = _clean_graph()
    graph.nodes["req:c.pay"] = _node("req:c.pay", "Requirement")
    edge = GraphEdge.create("req:c.pay", "delivered_by", "cap:BE-001")
    graph.edges[edge.id] = edge

    report = check_integrity(graph, RuleContext(change_id="c"))

    assert "requirement-without-capability" not in _rules_fired(report)


def test_an_acceptance_criterion_with_no_requirement_is_an_error():
    graph = _clean_graph()
    graph.nodes["ac:c.receipt"] = _node("ac:c.receipt", "AcceptanceCriterion")

    report = check_integrity(graph, RuleContext(change_id="c"))

    assert "criterion-without-requirement" in {f.rule for f in report.errors}


def test_a_criterion_refining_something_other_than_a_requirement_still_fires():
    """`refines` also joins Requirement to Requirement, so merely having one
    isn't enough — it has to reach a Requirement."""
    graph = _clean_graph()
    graph.nodes["ac:c.a"] = _node("ac:c.a", "AcceptanceCriterion")
    graph.nodes["ac:c.b"] = _node("ac:c.b", "AcceptanceCriterion")

    report = check_integrity(graph, RuleContext(change_id="c"))

    assert _subjects(report, "criterion-without-requirement") == ["ac:c.a", "ac:c.b"]


def test_a_technical_node_no_engineering_node_reaches_is_an_error():
    graph = _clean_graph()
    graph.nodes["mod:orphan"] = _node("mod:orphan", "Module",
                                      attributes={"path": "src/Orphan"})

    report = check_integrity(graph)

    assert _subjects(report, "unreachable-technical-node") == ["mod:orphan"]


def test_reachability_is_undirected():
    """`realized_in` points down and `verifies` points up; a directed walk in
    either direction alone would call half the technical layer unreachable."""
    graph = _clean_graph()
    graph.nodes["test:checkout"] = _node("test:checkout", "Test",
                                         attributes={"path": "tests/CheckoutTest.php"})
    edge = GraphEdge.create("test:checkout", "verifies", "cmp:checkout")
    graph.edges[edge.id] = edge

    report = check_integrity(graph)

    assert "unreachable-technical-node" not in _rules_fired(report)


def test_a_business_id_not_scoped_to_its_change_is_an_error():
    """Two changes both defining req:checkout collide unmergeably. Scoping is
    what makes per-change overlays safe."""
    graph = _clean_graph()
    graph.nodes["req:pay"] = _node("req:pay", "Requirement")
    edge = GraphEdge.create("req:pay", "delivered_by", "cap:BE-001")
    graph.edges[edge.id] = edge

    report = check_integrity(graph, RuleContext(change_id="checkout-guest"))

    finding = next(f for f in report.errors if f.rule == "business-id-scoped-to-change")
    assert "req:checkout-guest.pay" in finding.fix


def test_a_correctly_scoped_business_id_passes():
    graph = _clean_graph()
    graph.nodes["req:checkout-guest.pay"] = _node("req:checkout-guest.pay", "Requirement")
    edge = GraphEdge.create("req:checkout-guest.pay", "delivered_by", "cap:BE-001")
    graph.edges[edge.id] = edge

    report = check_integrity(graph, RuleContext(change_id="checkout-guest"))

    assert "business-id-scoped-to-change" not in _rules_fired(report)


def test_without_a_change_in_context_an_id_must_still_be_namespaced():
    """Weaker check, not no check: without knowing which change owns it, the
    rule can't demand a particular prefix, but an id with no namespace at all
    is still the collision this rule exists to prevent."""
    graph = _clean_graph()
    graph.nodes["req:pay"] = _node("req:pay", "Requirement")
    edge = GraphEdge.create("req:pay", "delivered_by", "cap:BE-001")
    graph.edges[edge.id] = edge

    report = check_integrity(graph)

    finding = next(f for f in report.errors if f.rule == "business-id-scoped-to-change")
    assert "not namespaced by any change" in finding.message


def test_a_namespaced_id_passes_without_a_change_in_context():
    graph = _clean_graph()
    graph.nodes["req:other-change.pay"] = _node("req:other-change.pay", "Requirement")
    edge = GraphEdge.create("req:other-change.pay", "delivered_by", "cap:BE-001")
    graph.edges[edge.id] = edge

    report = check_integrity(graph)

    assert "business-id-scoped-to-change" not in _rules_fired(report)


def test_another_changes_nodes_are_not_held_to_this_changes_prefix():
    """The bug this split exists to prevent: a graph validated for change B
    still holds change A's landed requirements, and holding B responsible for
    A's naming would make every apply after the first one fail."""
    graph = _clean_graph()
    for node_id in ("req:change-a.pay", "req:change-b.refund"):
        graph.nodes[node_id] = _node(node_id, "Requirement")
        edge = GraphEdge.create(node_id, "delivered_by", "cap:BE-001")
        graph.edges[edge.id] = edge

    report = check_integrity(graph, RuleContext(
        change_id="change-b", owned_node_ids=frozenset({"req:change-b.refund"})))

    assert "business-id-scoped-to-change" not in _rules_fired(report)


def test_an_owned_node_with_the_wrong_prefix_still_fires():
    graph = _clean_graph()
    graph.nodes["req:change-a.pay"] = _node("req:change-a.pay", "Requirement")
    edge = GraphEdge.create("req:change-a.pay", "delivered_by", "cap:BE-001")
    graph.edges[edge.id] = edge

    report = check_integrity(graph, RuleContext(
        change_id="change-b", owned_node_ids=frozenset({"req:change-a.pay"})))

    finding = next(f for f in report.errors if f.rule == "business-id-scoped-to-change")
    assert "not scoped to change 'change-b'" in finding.message


# -- warnings ------------------------------------------------------------

def test_a_capability_with_no_component_is_a_warning():
    # cap:BE-002 has an edge (so orphan-node stays quiet) but no
    # implemented_by, which is the thing under test.
    graph = _graph(
        _node("cap:BE-001", "Capability", attributes={"category": "BE"}),
        _node("cap:BE-002", "Capability", attributes={"category": "BE"}),
        _node("cmp:checkout", "Component"),
        edges=[GraphEdge.create("cap:BE-001", "implemented_by", "cmp:checkout"),
               GraphEdge.create("cap:BE-002", "depends_on", "cap:BE-001")])

    report = check_integrity(graph)

    assert _subjects(report, "capability-without-component") == ["cap:BE-002"]
    assert "capability-without-component" in {f.rule for f in report.warnings}


def test_an_orphan_node_is_a_warning():
    report = check_integrity(_graph(_node("ent:invoice", "DataEntity")))

    assert _subjects(report, "orphan-node") == ["ent:invoice"]
    assert "orphan-node" in {f.rule for f in report.warnings}


def test_an_empty_container_is_exempt_from_the_orphan_warning():
    """A legitimate starting state for a repo whose components aren't mapped
    yet. Warning here would train people to ignore the rule."""
    report = check_integrity(_graph(_node("ctr:api", "Container")))

    assert "orphan-node" not in _rules_fired(report)


def test_a_capability_with_no_manifest_is_a_warning_not_an_error():
    """The graph legitimately records a capability `capability recommend`
    proposed and nobody generated yet. Erroring would require the manifest to
    exist before the graph could describe the gap, which is backwards."""
    graph = _clean_graph()
    ctx = RuleContext(manifests={"DB-001": {"capability": {"id": "DB-001"}}})

    report = check_integrity(graph, ctx)

    assert "capability-not-in-catalog" in {f.rule for f in report.warnings}
    assert "capability-not-in-catalog" not in {f.rule for f in report.errors}


def test_no_manifests_in_context_means_no_catalog_findings():
    """Saying nothing beats saying everything is missing — a command run
    without a capabilities tree must not report every node as uncatalogued."""
    report = check_integrity(_clean_graph(), RuleContext())

    assert "capability-not-in-catalog" not in _rules_fired(report)


def test_a_manifest_dependency_missing_from_the_graph_is_a_warning():
    graph = _clean_graph()
    graph.nodes["cap:DB-001"] = _node("cap:DB-001", "Capability",
                                      attributes={"category": "DB"})
    edge = GraphEdge.create("cap:DB-001", "implemented_by", "cmp:checkout")
    graph.edges[edge.id] = edge

    ctx = RuleContext(manifests={
        "BE-001": {"capability": {"id": "BE-001",
                                  "dependencies": [{"capability": "DB-001"}]}},
        "DB-001": {"capability": {"id": "DB-001"}}})

    report = check_integrity(graph, ctx)

    finding = next(f for f in report.warnings
                   if f.rule == "capability-deps-disagree-with-manifest")
    assert "declares a dependency on DB-001" in finding.message


def test_a_graph_dependency_the_manifest_does_not_declare_is_a_warning():
    """Reported against the graph in both directions — the manifest is the
    source of truth and the graph mirrors it."""
    graph = _clean_graph()
    graph.nodes["cap:DB-001"] = _node("cap:DB-001", "Capability",
                                      attributes={"category": "DB"})
    for edge in (GraphEdge.create("cap:DB-001", "implemented_by", "cmp:checkout"),
                 GraphEdge.create("cap:BE-001", "depends_on", "cap:DB-001")):
        graph.edges[edge.id] = edge

    ctx = RuleContext(manifests={
        "BE-001": {"capability": {"id": "BE-001"}},
        "DB-001": {"capability": {"id": "DB-001"}}})

    report = check_integrity(graph, ctx)

    finding = next(f for f in report.warnings
                   if f.rule == "capability-deps-disagree-with-manifest")
    assert "does not declare" in finding.message
    assert "source of truth" in finding.fix


# -- advisories ----------------------------------------------------------

def test_low_confidence_is_an_advisory():
    graph = _clean_graph()
    graph.nodes["cmp:checkout"] = _node("cmp:checkout", "Component",
                                        confidence=LOW_CONFIDENCE - 1)

    report = check_integrity(graph)

    assert "low-confidence" in {f.rule for f in report.advisories}
    assert report.ok, "an advisory must never block"


def test_confidence_exactly_at_the_threshold_does_not_fire():
    graph = _clean_graph()
    graph.nodes["cmp:checkout"] = _node("cmp:checkout", "Component",
                                        confidence=LOW_CONFIDENCE)

    assert "low-confidence" not in _rules_fired(check_integrity(graph))


def test_an_agent_node_citing_nothing_is_an_advisory():
    """The anti-hallucination rule, and the machine half of what the
    c4-component-diagram skill asks for in prose."""
    graph = _clean_graph()
    graph.nodes["cmp:checkout"] = _node("cmp:checkout", "Component", source=AGENT)

    report = check_integrity(graph)

    finding = next(f for f in report.advisories if f.rule == "no-evidence")
    assert "cites nothing" in finding.message


def test_an_agent_node_with_evidence_does_not_fire():
    graph = _clean_graph()
    graph.nodes["cmp:checkout"] = _node("cmp:checkout", "Component",
                                        source=AGENT, evidence=CITED)

    assert "no-evidence" not in _rules_fired(check_integrity(graph))


def test_a_detector_node_citing_nothing_does_not_fire():
    """Only agent-authored records earn this scrutiny: a detector's output is
    already a measurement of the repository."""
    graph = _clean_graph()
    graph.nodes["cmp:checkout"] = _node(
        "cmp:checkout", "Component",
        source=GraphSource(kind="detector", ref="acsdd profile discover", at="2026-08-10"))

    assert "no-evidence" not in _rules_fired(check_integrity(graph))


def test_an_agent_edge_with_a_rationale_but_no_evidence_is_accepted():
    """A relationship can be argued for as well as cited — an edge is often a
    judgement rather than a location."""
    graph = _clean_graph()
    edge = GraphEdge.create("cap:BE-001", "implemented_by", "cmp:checkout",
                            source=AGENT, rationale="The endpoint lands here.")
    graph.edges[edge.id] = edge

    assert "no-evidence" not in _rules_fired(check_integrity(graph))


def test_a_deprecated_node_that_is_still_referenced_is_an_advisory():
    graph = _clean_graph()
    graph.nodes["cmp:checkout"] = _node("cmp:checkout", "Component", status="deprecated")

    report = check_integrity(graph)

    finding = next(f for f in report.advisories
                   if f.rule == "deprecated-node-still-referenced")
    assert "cap:BE-001" in finding.message


# -- the report and the table -------------------------------------------

def test_ok_tracks_errors_and_clean_also_tracks_warnings():
    graph = _graph(_node("ent:invoice", "DataEntity"))  # orphan -> warning only

    report = check_integrity(graph)

    assert report.ok is True, "a warning must not block apply"
    assert report.clean is False, "--strict must still fail on it"


def test_to_dict_reports_counts_alongside_the_findings():
    payload = check_integrity(_graph(_node("ent:invoice", "DataEntity"))).to_dict()

    assert payload["error_count"] == 0
    assert payload["warning_count"] == 1
    assert payload["warnings"][0]["rule"] == "orphan-node"
    assert "fix" in payload["warnings"][0]


def test_every_rule_declares_a_known_severity():
    for rule in RULES:
        assert rule.severity in SEVERITIES, rule.name


def test_rule_names_are_unique():
    names = [r.name for r in RULES]
    assert len(names) == len(set(names)), sorted(names)


def test_every_rule_describes_itself():
    """The description is published in `graph context --json`, so an agent
    knows the stakes before it writes a changeset rather than discovering them
    at apply time. An empty one is a silent gap."""
    for rule in RULES:
        assert rule.describes, rule.name
        assert not rule.describes.endswith("."), rule.name


def test_every_rule_can_run_against_an_empty_graph():
    """A rule that assumes a non-empty graph fails on the first-ever run, which
    is exactly when a user is least able to interpret it."""
    report = check_integrity(Graph())

    assert report.errors == [] and report.warnings == [] and report.advisories == []


# -- the repo's own shipped example graph -------------------------------

def _real_graph():
    import json
    from pathlib import Path

    from acsdd.graph.model import Graph

    path = Path(__file__).parent.parent / ".acsdd" / "graph" / "graph.json"
    return Graph.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _real_manifests():
    from pathlib import Path

    from acsdd.capability.loader import iter_manifests

    manifests_dir = Path(__file__).parent.parent / ".acsdd" / "capabilities" / "_manifests"
    return {data["capability"]["id"]: data for _, data in iter_manifests(manifests_dir)}


def test_the_shipped_example_graph_is_clean():
    """acsdd ships a real graph of its own architecture so `graph show` and
    `graph validate` do something the moment the tool is installed. Same deal
    as the example manifests: editing it can break this test even when no tool
    code moved, and that is the intent — a shipped example that does not
    validate teaches the wrong thing."""
    report = check_integrity(_real_graph(), RuleContext(manifests=_real_manifests()))

    assert report.errors == [], [f.message for f in report.errors]
    assert report.warnings == [], [f.message for f in report.warnings]
    assert report.advisories == [], [f.message for f in report.advisories]


def test_the_shipped_example_graph_agrees_with_the_real_manifests():
    """The Capability nodes and their depends_on edges mirror
    .acsdd/capabilities/_manifests/. If a manifest gains a dependency and the
    graph does not, this is what says so."""
    graph = _real_graph()
    manifests = _real_manifests()

    graph_ids = {n.id.split(":", 1)[-1] for n in graph.nodes_of_type("Capability")}
    assert graph_ids == set(manifests)


def test_the_shipped_example_graph_matches_the_schema():
    import json
    from pathlib import Path

    from acsdd.graph.validator import validate_graph_document

    path = Path(__file__).parent.parent / ".acsdd" / "graph" / "graph.json"
    result = validate_graph_document(json.loads(path.read_text(encoding="utf-8")))

    assert result.ok, result.errors


def test_the_shipped_example_graph_is_canonically_serialized():
    """It is checked in, so it has to be the exact bytes acsdd would write —
    otherwise the first `graph apply` in this repo produces a spurious diff."""
    import json
    from pathlib import Path

    from acsdd.graph.model import serialize

    path = Path(__file__).parent.parent / ".acsdd" / "graph" / "graph.json"
    on_disk = path.read_text(encoding="utf-8")
    generated_at = json.loads(on_disk)["graph"]["meta"]["generated_at"]

    from datetime import date
    rewritten = serialize(_real_graph(), generated_at=date.fromisoformat(generated_at),
                          acsdd_version=json.loads(on_disk)["graph"]["meta"].get(
                              "acsdd_version", ""))

    assert rewritten == on_disk


# -- the shared cycle helper --------------------------------------------

def test_find_cycle_returns_the_path():
    adjacency = {"a": ["b"], "b": ["c"], "c": ["a"]}

    assert find_cycle(adjacency, "a") == ["a", "b", "c", "a"]


def test_find_cycle_returns_none_for_a_dag():
    assert find_cycle({"a": ["b"], "b": ["c"]}, "a") is None


@pytest.mark.parametrize("adjacency", [{}, {"a": []}, {"a": ["b"]}])
def test_find_cycle_tolerates_missing_and_empty_nodes(adjacency):
    assert find_cycle(adjacency, "a") is None
