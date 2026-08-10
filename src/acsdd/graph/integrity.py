"""Everything about a graph that a single-file JSON Schema cannot express.

Exactly `capability.validator.validate_catalog`'s charter, generalized: the
schema owns the shape of one record, this owns the relationships between them.
The allowed-edge matrix lives in `graph.vocabulary` and is read here rather
than encoded in the schema, because expressing it in JSON Schema means an
``allOf`` of ten ``if/then`` clauses keyed on id *prefixes* — which would
enforce a naming convention rather than a type relation.

**Severity is carried by the rule, not by the finding.** A finding says what is
wrong and how to fix it; `IntegrityRule.severity` decides which of the report's
three lists receives it. That keeps the repo's "severity is which list it lands
in" convention while letting `graph context --json` publish the whole table, so
an agent knows the stakes of each rule *before* it writes a changeset rather
than discovering them at apply time.

The three lists mean three different things, and the distinction is what keeps
the report worth reading:

- **errors** block `graph apply` and exit 1 from `graph validate`.
- **warnings** are reported and do not block; they fail `validate --strict`.
- **advisories** never block and never affect an exit code.

Rules read the graph and a `RuleContext` and nothing else. The context is
explicit precisely so no rule can reach for the filesystem — `recommender.py`'s
no-detector-re-run discipline, here enforced by the signature.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, List, Optional, Set, Tuple

from acsdd.graph import vocabulary
from acsdd.graph.model import Graph

SEVERITIES = ("error", "warning", "advisory")


@dataclass(frozen=True)
class IntegrityFinding:
    rule: str
    subject: str
    message: str
    fix: str = ""

    def to_dict(self) -> Dict:
        out: Dict = {"rule": self.rule, "subject": self.subject,
                     "message": self.message}
        if self.fix:
            out["fix"] = self.fix
        return out


@dataclass(frozen=True)
class RuleContext:
    """Everything a rule may consult besides the graph itself.

    `manifests` is `{capability_id: raw_manifest_dict}` as `cli._load_all`
    produces it. `change_id` is set when validating a change overlay, and
    `owned_node_ids` is the set of nodes that change introduces.

    The pair matters: a graph being validated for change B still contains
    change A's already-landed requirements, and holding B responsible for A's
    naming would make every apply after the first one fail. `owned_node_ids`
    is what draws that line. `None` means "we don't know which are ours", and
    the scoping rule falls back to the weaker check that every business id is
    namespaced by *something*.
    """

    manifests: Dict[str, Dict] = field(default_factory=dict)
    profile: Dict = field(default_factory=dict)
    change_id: Optional[str] = None
    owned_node_ids: Optional[FrozenSet[str]] = None


@dataclass(frozen=True)
class IntegrityRule:
    name: str
    severity: str
    describes: str
    check: Callable[[Graph, RuleContext], List[IntegrityFinding]]

    def to_dict(self) -> Dict:
        return {"name": self.name, "severity": self.severity,
                "describes": self.describes}


@dataclass
class IntegrityReport:
    errors: List[IntegrityFinding] = field(default_factory=list)
    warnings: List[IntegrityFinding] = field(default_factory=list)
    advisories: List[IntegrityFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def clean(self) -> bool:
        """No errors *and* no warnings — what ``--strict`` requires."""
        return not self.errors and not self.warnings

    def to_dict(self) -> Dict:
        return {
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "advisory_count": len(self.advisories),
            "errors": [f.to_dict() for f in self.errors],
            "warnings": [f.to_dict() for f in self.warnings],
            "advisories": [f.to_dict() for f in self.advisories],
        }


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------

def find_cycle(adjacency: Dict[str, List[str]], start: str) -> Optional[List[str]]:
    """The path of a cycle through `start`, or None.

    A knowing copy of the explicit-stack DFS in
    `capability.validator.validate_catalog`, generalized to an adjacency map.
    That one is deliberately left alone: its traversal is hardcoded to
    ``capability.dependencies`` and its message string is part of a contract
    `catalog verify` and `test_validate_catalog_circular_dependency` assert on,
    so refactoring it would be a behaviour-neutral change to a stable, tested
    module in service of a subsystem that did not exist yet.

    **If a third copy of this appears, extract all three into a shared module.**
    """
    stack: List[Tuple[str, List[str]]] = [(start, [start])]
    while stack:
        node, path = stack.pop()
        for neighbour in adjacency.get(node, []):
            if neighbour == start:
                return path + [neighbour]
            if neighbour not in path:
                stack.append((neighbour, path + [neighbour]))
    return None


def _known(graph: Graph, node_id: str) -> bool:
    node = graph.nodes.get(node_id)
    return node is not None and node.type in vocabulary.NODE_TYPES


def _type_of(graph: Graph, node_id: str) -> Optional[str]:
    node = graph.nodes.get(node_id)
    return node.type if node is not None else None


# ---------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------

def _check_id_prefix_matches_type(graph: Graph, ctx: RuleContext) -> List[IntegrityFinding]:
    """A node id whose prefix names a different type than the node declares.

    Makes an id readable as a type in a raw diff — ``cmp:`` is always a
    Component — which is what lets a reviewer check an edge against the matrix
    without looking anything up.
    """
    found = []
    by_prefix = vocabulary.prefix_to_type()
    for node in graph.nodes.values():
        if node.type not in vocabulary.NODE_TYPES:
            continue  # unknown-type says this already
        expected = vocabulary.NODE_TYPES[node.type].id_prefix
        prefix = node.id.split(":", 1)[0] if ":" in node.id else ""
        if prefix == expected:
            continue
        actual = by_prefix.get(prefix)
        found.append(IntegrityFinding(
            rule="id-prefix-matches-type", subject=node.id,
            message=(f"id prefix '{prefix}:' names "
                     f"{actual or 'no known type'}, but the node declares "
                     f"type '{node.type}'"),
            fix=f"rename the node to '{expected}:{node.id.split(':', 1)[-1]}'"))
    return sorted(found, key=lambda f: f.subject)


def _check_unknown_type(graph: Graph, ctx: RuleContext) -> List[IntegrityFinding]:
    found = []
    for node in sorted(graph.nodes.values(), key=lambda n: n.id):
        if node.type not in vocabulary.NODE_TYPES:
            found.append(IntegrityFinding(
                rule="unknown-type", subject=node.id,
                message=f"node type '{node.type}' is not in the vocabulary",
                fix=f"use one of: {', '.join(sorted(vocabulary.NODE_TYPES))}"))
    for edge in sorted(graph.edges.values(), key=lambda e: e.id):
        if edge.type not in vocabulary.EDGE_TYPES:
            found.append(IntegrityFinding(
                rule="unknown-type", subject=edge.id,
                message=f"edge type '{edge.type}' is not in the vocabulary",
                fix=f"use one of: {', '.join(sorted(vocabulary.EDGE_TYPES))}"))
    return found


def _check_dangling_edge(graph: Graph, ctx: RuleContext) -> List[IntegrityFinding]:
    found = []
    for edge in sorted(graph.edges.values(), key=lambda e: e.id):
        for role, node_id in (("from", edge.from_id), ("to", edge.to_id)):
            if node_id not in graph.nodes:
                found.append(IntegrityFinding(
                    rule="dangling-edge", subject=edge.id,
                    message=f"'{role}' points at '{node_id}', which is not in the graph",
                    fix=f"add a node for '{node_id}', or remove this edge"))
    return found


def _check_illegal_edge_pair(graph: Graph, ctx: RuleContext) -> List[IntegrityFinding]:
    """The layer-crossing rule.

    It needs no separate implementation because layer is a function of type: a
    pair the matrix permits is by construction a legal crossing, and one it
    does not is caught here whether the types share a layer or not.
    """
    found = []
    for edge in sorted(graph.edges.values(), key=lambda e: e.id):
        if edge.type not in vocabulary.EDGE_TYPES:
            continue  # unknown-type says this already
        from_type = _type_of(graph, edge.from_id)
        to_type = _type_of(graph, edge.to_id)
        if from_type is None or to_type is None:
            continue  # dangling-edge says this already
        if from_type not in vocabulary.NODE_TYPES or to_type not in vocabulary.NODE_TYPES:
            continue
        if vocabulary.edge_pair_allowed(from_type, edge.type, to_type):
            continue
        legal = vocabulary.EDGE_TYPES[edge.type].pairs
        found.append(IntegrityFinding(
            rule="illegal-edge-pair", subject=edge.id,
            message=(f"'{edge.type}' does not join {from_type} to {to_type} "
                     f"({vocabulary.layer_of(from_type)} -> "
                     f"{vocabulary.layer_of(to_type)})"),
            fix=("'" + edge.type + "' joins: "
                 + "; ".join(f"{a} -> {b}" for a, b in legal))))
    return found


def _check_cycle(graph: Graph, ctx: RuleContext) -> List[IntegrityFinding]:
    found = []
    for name, spec in sorted(vocabulary.EDGE_TYPES.items()):
        if not spec.acyclic:
            continue
        adjacency: Dict[str, List[str]] = {}
        for edge in graph.edges.values():
            if edge.type == name:
                adjacency.setdefault(edge.from_id, []).append(edge.to_id)

        reported: Set[frozenset] = set()
        for node_id in sorted(adjacency):
            cycle = find_cycle(adjacency, node_id)
            if not cycle:
                continue
            key = frozenset(cycle)
            if key in reported:
                continue  # the same loop, entered from a different node
            reported.add(key)
            found.append(IntegrityFinding(
                rule="cycle", subject=node_id,
                message=f"circular '{name}': {' -> '.join(cycle)}",
                fix=f"'{name}' must stay acyclic — break one of these edges"))
    return found


def _check_requirement_without_capability(graph: Graph,
                                          ctx: RuleContext) -> List[IntegrityFinding]:
    found = []
    for node in graph.nodes_of_type("Requirement"):
        if not graph.out_edges(node.id, "delivered_by"):
            found.append(IntegrityFinding(
                rule="requirement-without-capability", subject=node.id,
                message=f"'{node.name}' has no capability that delivers it",
                fix=("add a 'delivered_by' edge to a Capability, or record the "
                     "gap — run `acsdd capability recommend` to see which "
                     "capability this repo is missing")))
    return found


def _check_criterion_without_requirement(graph: Graph,
                                         ctx: RuleContext) -> List[IntegrityFinding]:
    found = []
    for node in graph.nodes_of_type("AcceptanceCriterion"):
        refines = [e for e in graph.out_edges(node.id, "refines")
                   if _type_of(graph, e.to_id) == "Requirement"]
        if not refines:
            found.append(IntegrityFinding(
                rule="criterion-without-requirement", subject=node.id,
                message=f"'{node.name}' refines no Requirement",
                fix="add a 'refines' edge to the Requirement this criterion tests"))
    return found


def _check_unreachable_technical_node(graph: Graph,
                                      ctx: RuleContext) -> List[IntegrityFinding]:
    """A Module or Test no engineering-layer node reaches.

    Undirected reachability on purpose: `realized_in` points down from a
    Component to its Module and `verifies` points up from a Test, so a directed
    walk in either direction alone would call half the technical layer
    unreachable.
    """
    seeds = [n.id for n in graph.nodes.values()
             if n.type in vocabulary.NODE_TYPES
             and vocabulary.layer_of(n.type) == "engineering"]

    seen: Set[str] = set()
    stack = list(seeds)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(graph.neighbours(current))

    found = []
    for node in sorted(graph.nodes.values(), key=lambda n: n.id):
        if node.type not in vocabulary.NODE_TYPES:
            continue
        if vocabulary.layer_of(node.type) != "technical":
            continue
        if node.id in seen:
            continue
        found.append(IntegrityFinding(
            rule="unreachable-technical-node", subject=node.id,
            message=(f"{node.type} '{node.name}' is not reachable from any "
                     f"engineering-layer node"),
            fix=("link it to what it implements — a Component 'realized_in' "
                 "this module, or a Test that 'verifies' something")))
    return found


def _check_business_id_scoped_to_change(graph: Graph,
                                        ctx: RuleContext) -> List[IntegrityFinding]:
    """Business node ids must be namespaced by their change.

    Not in the original design, and load-bearing: with per-change overlays, two
    changes both defining ``req:checkout`` collide unmergeably and undetectably.
    Ids are ``req:<change-slug>.<slug>``.

    Two levels, because a graph validated for change B still holds change A's
    already-landed requirements:

    - nodes this change **owns** must carry *this* change's prefix;
    - every other business node need only be namespaced by something, which its
      own change already checked when it landed.

    Without that split, the second change applied to any repository would fail
    on the first change's perfectly good ids.
    """
    found = []
    prefix = f"{ctx.change_id}." if ctx.change_id else None

    for node in sorted(graph.nodes.values(), key=lambda n: n.id):
        if node.type not in vocabulary.NODE_TYPES:
            continue
        if vocabulary.layer_of(node.type) != "business":
            continue

        local = node.id.split(":", 1)[-1]
        node_prefix = node.id.split(":", 1)[0]
        ours = ctx.owned_node_ids is None or node.id in ctx.owned_node_ids

        if ours and prefix is not None:
            if local.startswith(prefix):
                continue
            found.append(IntegrityFinding(
                rule="business-id-scoped-to-change", subject=node.id,
                message=f"business node id is not scoped to change '{ctx.change_id}'",
                fix=f"rename it to '{node_prefix}:{prefix}{local}'"))
            continue

        # Someone else's node, or no change in context: it only has to be
        # namespaced by *a* change, not by this one.
        if "." in local:
            continue
        found.append(IntegrityFinding(
            rule="business-id-scoped-to-change", subject=node.id,
            message="business node id is not namespaced by any change",
            fix=f"rename it to '{node_prefix}:<change-id>.{local}'"))
    return found


def _check_required_attributes(graph: Graph, ctx: RuleContext) -> List[IntegrityFinding]:
    found = []
    for node in sorted(graph.nodes.values(), key=lambda n: n.id):
        spec = vocabulary.NODE_TYPES.get(node.type)
        if spec is None:
            continue
        for key in spec.required_attributes:
            if node.attributes.get(key):
                continue
            found.append(IntegrityFinding(
                rule="required-attribute", subject=node.id,
                message=f"{node.type} requires an '{key}' attribute",
                fix=f"set attributes.{key} on '{node.id}'"))
    return found


# ---------------------------------------------------------------------
# warnings
# ---------------------------------------------------------------------

def _check_capability_without_component(graph: Graph,
                                        ctx: RuleContext) -> List[IntegrityFinding]:
    found = []
    for node in graph.nodes_of_type("Capability"):
        if not graph.out_edges(node.id, "implemented_by"):
            found.append(IntegrityFinding(
                rule="capability-without-component", subject=node.id,
                message=f"'{node.name}' resolves to no component in this repository",
                fix=("add an 'implemented_by' edge to the Component an agent "
                     "would actually edit")))
    return found


def _check_orphan_node(graph: Graph, ctx: RuleContext) -> List[IntegrityFinding]:
    """Nodes with no edges at all.

    `Container` is exempt: an empty container is a legitimate starting state
    for a repository whose components have not been mapped yet, and warning
    about it on the first import would train people to ignore the rule.
    """
    found = []
    for node in sorted(graph.nodes.values(), key=lambda n: n.id):
        if node.type == "Container":
            continue
        if graph.out_edges(node.id) or graph.in_edges(node.id):
            continue
        found.append(IntegrityFinding(
            rule="orphan-node", subject=node.id,
            message=f"{node.type} '{node.name}' has no edges in either direction",
            fix="link it to something, or remove it — an unconnected node is unreachable"))
    return found


def _check_capability_not_in_catalog(graph: Graph,
                                     ctx: RuleContext) -> List[IntegrityFinding]:
    """A Capability node with no manifest behind it.

    A *warning* and not an error, deliberately: the graph legitimately records
    a capability `acsdd capability recommend` proposed and nobody has generated
    yet. Making it an error would force the manifest to exist before the graph
    could describe the gap, which is backwards.
    """
    if not ctx.manifests:
        return []  # nothing to check against; say nothing rather than everything

    found = []
    for node in graph.nodes_of_type("Capability"):
        manifest_id = node.id.split(":", 1)[-1]
        if manifest_id in ctx.manifests:
            continue
        found.append(IntegrityFinding(
            rule="capability-not-in-catalog", subject=node.id,
            message=f"no manifest '{manifest_id}' exists in this repository",
            fix=(f"run `acsdd capability generate --id {manifest_id} "
                 f"--category {node.attributes.get('category', 'CAT')}`, or "
                 f"remove the node if the capability was never created")))
    return found


def _check_capability_deps_disagree(graph: Graph,
                                    ctx: RuleContext) -> List[IntegrityFinding]:
    """Capability `depends_on` edges that the manifests do not corroborate.

    The manifest is the source of truth and the graph mirrors it, so both
    directions of disagreement are reported against the graph.
    """
    if not ctx.manifests:
        return []

    found = []
    for node in graph.nodes_of_type("Capability"):
        manifest_id = node.id.split(":", 1)[-1]
        manifest = ctx.manifests.get(manifest_id)
        if manifest is None:
            continue  # capability-not-in-catalog says this already

        declared = {d.get("capability") for d
                    in (manifest.get("capability", {}).get("dependencies") or [])
                    if d.get("capability")}
        in_graph = {e.to_id.split(":", 1)[-1]
                    for e in graph.out_edges(node.id, "depends_on")}

        for missing in sorted(declared - in_graph):
            found.append(IntegrityFinding(
                rule="capability-deps-disagree-with-manifest", subject=node.id,
                message=(f"manifest {manifest_id} declares a dependency on "
                         f"{missing}, which the graph does not record"),
                fix=f"add 'cap:{manifest_id} depends_on cap:{missing}'"))
        for extra in sorted(in_graph - declared):
            found.append(IntegrityFinding(
                rule="capability-deps-disagree-with-manifest", subject=node.id,
                message=(f"the graph records a dependency on {extra}, which "
                         f"manifest {manifest_id} does not declare"),
                fix=(f"add it to {manifest_id}.yaml's dependencies, or remove "
                     f"the edge — the manifest is the source of truth")))
    return found


# ---------------------------------------------------------------------
# advisories
# ---------------------------------------------------------------------

def _check_low_confidence(graph: Graph, ctx: RuleContext) -> List[IntegrityFinding]:
    found = []
    for node in sorted(graph.nodes.values(), key=lambda n: n.id):
        if node.confidence < vocabulary.LOW_CONFIDENCE:
            found.append(IntegrityFinding(
                rule="low-confidence", subject=node.id,
                message=f"confidence {node.confidence} — asserted, not established",
                fix="confirm it against the repository, then raise the confidence"))
    for edge in sorted(graph.edges.values(), key=lambda e: e.id):
        if edge.confidence < vocabulary.LOW_CONFIDENCE:
            found.append(IntegrityFinding(
                rule="low-confidence", subject=edge.id,
                message=f"confidence {edge.confidence} — asserted, not established",
                fix="confirm the relationship, then raise the confidence"))
    return found


def _check_no_evidence(graph: Graph, ctx: RuleContext) -> List[IntegrityFinding]:
    """Agent-authored records citing nothing.

    The anti-hallucination rule, and the machine half of what the
    c4-component-diagram skill asks for in prose: every component that is not
    new cites a real path. It stays an advisory because a warning-blocking
    apply would train people to reach for ``--force`` reflexively, which would
    also defeat the base-revision check. The blocking gate is procedural, in
    the graph-import skill's propose-then-stop step.
    """
    found = []
    for node in sorted(graph.nodes.values(), key=lambda n: n.id):
        if node.source is None or node.source.kind != "agent":
            continue
        if node.evidence:
            continue
        found.append(IntegrityFinding(
            rule="no-evidence", subject=node.id,
            message=f"{node.type} '{node.name}' was proposed by an agent and cites nothing",
            fix="add evidence with the file path (and lines) it was read from"))
    for edge in sorted(graph.edges.values(), key=lambda e: e.id):
        if edge.source is None or edge.source.kind != "agent":
            continue
        if edge.evidence or edge.rationale:
            continue
        found.append(IntegrityFinding(
            rule="no-evidence", subject=edge.id,
            message="edge was proposed by an agent with neither evidence nor a rationale",
            fix="add evidence, or state why the relationship holds"))
    return found


def _check_deprecated_still_referenced(graph: Graph,
                                       ctx: RuleContext) -> List[IntegrityFinding]:
    found = []
    for node in sorted(graph.nodes.values(), key=lambda n: n.id):
        if node.status != "deprecated":
            continue
        referrers = [e.from_id for e in graph.in_edges(node.id)]
        if not referrers:
            continue
        found.append(IntegrityFinding(
            rule="deprecated-node-still-referenced", subject=node.id,
            message=(f"deprecated, but still referenced by "
                     f"{', '.join(sorted(set(referrers)))}"),
            fix="migrate the referrers, or drop the deprecated status"))
    return found


# ---------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------

RULES: Tuple[IntegrityRule, ...] = (
    # --- errors: block apply -------------------------------------------
    IntegrityRule(
        "unknown-type", "error",
        "every node and edge type is in the vocabulary",
        _check_unknown_type),
    IntegrityRule(
        "id-prefix-matches-type", "error",
        "a node id's prefix names the type the node declares",
        _check_id_prefix_matches_type),
    IntegrityRule(
        "required-attribute", "error",
        "a node type's required attributes are all set",
        _check_required_attributes),
    IntegrityRule(
        "dangling-edge", "error",
        "both endpoints of every edge exist in the graph",
        _check_dangling_edge),
    IntegrityRule(
        "illegal-edge-pair", "error",
        "every edge joins a node-type pair the vocabulary matrix permits — "
        "this is also the layer-crossing rule",
        _check_illegal_edge_pair),
    IntegrityRule(
        "cycle", "error",
        "edge types declared acyclic (refines, depends_on, contains) contain no cycle",
        _check_cycle),
    IntegrityRule(
        "requirement-without-capability", "error",
        "every Requirement has at least one delivered_by edge to a Capability",
        _check_requirement_without_capability),
    IntegrityRule(
        "criterion-without-requirement", "error",
        "every AcceptanceCriterion refines at least one Requirement",
        _check_criterion_without_requirement),
    IntegrityRule(
        "unreachable-technical-node", "error",
        "every Module and Test is reachable from the engineering layer",
        _check_unreachable_technical_node),
    IntegrityRule(
        "business-id-scoped-to-change", "error",
        "business node ids are namespaced by their change id, so two changes "
        "cannot collide",
        _check_business_id_scoped_to_change),

    # --- warnings: reported, do not block ------------------------------
    IntegrityRule(
        "capability-without-component", "warning",
        "every Capability resolves to at least one Component",
        _check_capability_without_component),
    IntegrityRule(
        "capability-not-in-catalog", "warning",
        "every Capability node has a manifest behind it",
        _check_capability_not_in_catalog),
    IntegrityRule(
        "capability-deps-disagree-with-manifest", "warning",
        "Capability depends_on edges agree with the manifests, which are the "
        "source of truth",
        _check_capability_deps_disagree),
    IntegrityRule(
        "orphan-node", "warning",
        "no node is left with no edges in either direction (Container exempt)",
        _check_orphan_node),

    # --- advisories: never block ---------------------------------------
    IntegrityRule(
        "low-confidence", "advisory",
        f"nodes and edges below confidence {vocabulary.LOW_CONFIDENCE} are "
        f"flagged as asserted rather than established",
        _check_low_confidence),
    IntegrityRule(
        "no-evidence", "advisory",
        "agent-authored nodes and edges cite a file path",
        _check_no_evidence),
    IntegrityRule(
        "deprecated-node-still-referenced", "advisory",
        "nothing still points at a deprecated node",
        _check_deprecated_still_referenced),
)


def check_integrity(graph: Graph, ctx: Optional[RuleContext] = None) -> IntegrityReport:
    """Run every rule and sort the findings into the report's three lists."""
    context = ctx or RuleContext()
    report = IntegrityReport()
    bucket = {"error": report.errors, "warning": report.warnings,
              "advisory": report.advisories}

    for rule in RULES:
        bucket[rule.severity].extend(rule.check(graph, context))

    return report
