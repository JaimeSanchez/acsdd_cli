"""The closed vocabulary of the engineering knowledge graph: which node types
exist, which edge types exist, and which of them may legally connect.

This is a **permanent API**, and the most expensive table in the codebase to
get wrong. Node ids embed their type prefix (``cmp:payment-retry``) and land in
consumers' checked-in ``graph.json``, so adding a type is a minor version bump
while renaming or removing one is a major one. That is why there are thirteen
node types rather than forty: every one here is the ``from`` or ``to`` of at
least one entry in the edge matrix, and every one answers a question some rule
or projection actually asks. A type nothing can connect to is a dead type, and
`tests/test_graph_vocabulary.py` fails if one appears.

Static data only — no I/O, no ``jsonschema`` import, no dataclass logic beyond
the records themselves. The same split `catalog.builder.CATEGORY_ORDER` has
from `capability.validator`: four consumers read this module (the schema guard
tests, `graph.integrity`, `graph.context`, `graph.report`), and none of them
should have to import a validator to answer "what node types exist".

**The allowed-edge matrix lives here and not in the JSON Schema.** Expressing
it in schema means an ``allOf`` of ten ``if/then`` clauses keyed on id
*prefixes* — unreadable, and wrong: it would enforce a naming convention rather
than a type relation. It belongs with the other checks a single-file schema
can't express, which is `graph.integrity`, reading this table.
"""

import re
from dataclasses import dataclass
from typing import Dict, Tuple

# ``<prefix>:<local-part>``. The prefix names the node type (see NODE_TYPES),
# which is what makes a raw graph.json diff readable without a lookup.
NODE_ID_PATTERN = r"^[a-z]{2,4}:[A-Za-z0-9][A-Za-z0-9._-]{1,63}$"
NODE_ID_RE = re.compile(NODE_ID_PATTERN)

LAYERS = ("business", "engineering", "technical")


@dataclass(frozen=True)
class NodeType:
    """One node type.

    `durable` is the repo-graph/change-overlay split: a durable type describes
    the repository and belongs in ``.acsdd/graph/graph.json``; a non-durable
    one describes one change's intent and belongs in that change's overlay. A
    Requirement is never durable — it is satisfied and then it is history.

    `attributes` is advisory (it tells an agent which keys are meaningful);
    `required_attributes` is enforced by `graph.integrity`.
    """

    name: str
    layer: str
    id_prefix: str
    durable: bool
    describes: str
    attributes: Tuple[str, ...] = ()
    required_attributes: Tuple[str, ...] = ()


NODE_TYPES: Dict[str, NodeType] = {
    # --- business: authored per change, never durable ---------------------
    "Requirement": NodeType(
        "Requirement", "business", "req", False,
        "A behaviour of the product a stakeholder asked for.",
        attributes=("actor", "priority", "prd_ref")),
    "AcceptanceCriterion": NodeType(
        "AcceptanceCriterion", "business", "ac", False,
        "A testable condition that decides whether a Requirement is met.",
        attributes=("given", "when", "then")),
    "BusinessRule": NodeType(
        "BusinessRule", "business", "rule", False,
        "An invariant of the domain that holds regardless of any one requirement."),
    "Constraint": NodeType(
        "Constraint", "business", "con", False,
        "A non-functional or regulatory limit on how the change may be built.",
        attributes=("kind",)),  # security | performance | compliance | operational

    # --- engineering: durable ---------------------------------------------
    "Capability": NodeType(
        "Capability", "engineering", "cap", True,
        "An ACSDD capability manifest. The id's local part is the manifest id "
        "(cap:DB-001 <-> _manifests/DB-001.yaml).",
        attributes=("category", "manifest_version"),
        required_attributes=("category",)),
    "Container": NodeType(
        "Container", "engineering", "ctr", True,
        "A deployable unit — the boundary of one C4 Component diagram.",
        attributes=("technology",)),
    "Component": NodeType(
        "Component", "engineering", "cmp", True,
        "A logical unit owning one responsibility. The C4 Component: if renaming "
        "it would not change how you describe the system, it is a class, not a "
        "component.",
        attributes=("technology",)),
    "Interface": NodeType(
        "Interface", "engineering", "ifc", True,
        "A contract surface: an HTTP route, a CLI command, a queue topic, an event.",
        attributes=("protocol", "signature")),
    "DataEntity": NodeType(
        "DataEntity", "engineering", "ent", True,
        "A domain entity, aggregate, or table.",
        attributes=("persistence",)),
    "QualityGate": NodeType(
        "QualityGate", "engineering", "gate", True,
        "A check that must pass. Mirrors a profile quality_gates entry or a "
        "manifest's quality_gates entry.",
        attributes=("command", "automatic")),
    "ExternalSystem": NodeType(
        "ExternalSystem", "engineering", "ext", True,
        "A system outside every Container described here. C4's System_Ext."),

    # --- technical: durable ------------------------------------------------
    "Module": NodeType(
        "Module", "technical", "mod", True,
        "A directory, package, or namespace in the repository.",
        attributes=("path", "language"),
        required_attributes=("path",)),
    "Test": NodeType(
        "Test", "technical", "test", True,
        "A test suite or file proving something above it holds.",
        attributes=("path", "framework")),
}

# There is deliberately no `File` node type. A node per file is a graph nobody
# reviews and a diff nobody reads; `Module` plus GraphEvidence.path already
# carries file-level precision at a fraction of the node count. It can be added
# later without a break precisely because the enum lives in exactly one place.


@dataclass(frozen=True)
class EdgeType:
    """One edge type and the complete set of node-type pairs it may join.

    `pairs` **is** the allowed-edge matrix. It keys on node *type* alone and
    never on ``(layer, type)``: layer is a function of type
    (``NODE_TYPES[t].layer``), so a matrix keyed on both would encode two facts
    that can disagree and would need its own rule to police the disagreement.

    `reading` spells the edge out as a sentence ("FROM <verb> TO") for humans
    and for the `graph context --json` payload, so a skill never has to guess
    the direction.
    """

    name: str
    reading: str
    pairs: Tuple[Tuple[str, str], ...]
    acyclic: bool = False
    describes: str = ""


EDGE_TYPES: Dict[str, EdgeType] = {
    "refines": EdgeType(
        "refines", "FROM narrows the meaning of TO", acyclic=True,
        describes="Decomposition within the business layer.",
        pairs=(
            ("AcceptanceCriterion", "Requirement"),
            ("Requirement", "Requirement"),
        )),
    "governs": EdgeType(
        "governs", "FROM constrains what TO may do",
        describes="A rule, limit, or gate applying to something else.",
        pairs=(
            ("BusinessRule", "Requirement"),
            ("BusinessRule", "Capability"),
            ("BusinessRule", "Component"),
            ("BusinessRule", "DataEntity"),
            ("Constraint", "Capability"),
            ("Constraint", "Component"),
            ("Constraint", "Container"),
            ("QualityGate", "Capability"),
            ("QualityGate", "Component"),
        )),
    "delivered_by": EdgeType(
        "delivered_by", "TO is the capability that must run to deliver FROM",
        describes="The business-to-engineering hand-off. A Requirement with "
                  "none of these is a requirement nobody can act on.",
        pairs=(
            ("Requirement", "Capability"),
        )),
    "implemented_by": EdgeType(
        "implemented_by", "TO is where FROM lands in this repository",
        describes="Which component an agent would actually edit to exercise "
                  "the capability.",
        pairs=(
            ("Capability", "Component"),
        )),
    "depends_on": EdgeType(
        "depends_on", "FROM cannot work without TO", acyclic=True,
        describes="Mirrors a manifest's dependencies for Capability pairs; the "
                  "manifest stays the source of truth.",
        pairs=(
            ("Capability", "Capability"),
            ("Component", "Component"),
            ("Component", "ExternalSystem"),
            ("Module", "Module"),
        )),
    "exposes": EdgeType(
        "exposes", "FROM offers TO as a contract surface",
        describes="What breaking TO would break outside FROM.",
        pairs=(
            ("Component", "Interface"),
            ("Container", "Interface"),
        )),
    "owns": EdgeType(
        "owns", "FROM is the single writer of TO",
        describes="Deliberately singular: two owners of one entity is a "
                  "finding, not a modelling choice.",
        pairs=(
            ("Component", "DataEntity"),
        )),
    "realized_in": EdgeType(
        "realized_in", "FROM's code lives in TO",
        describes="The engineering-to-technical hand-off.",
        pairs=(
            ("Component", "Module"),
            ("DataEntity", "Module"),
            ("Interface", "Module"),
            ("Test", "Module"),
        )),
    "verifies": EdgeType(
        "verifies", "FROM proves TO holds",
        describes="The one upward edge, and what makes 'which tests prove this "
                  "acceptance criterion' answerable.",
        pairs=(
            ("Test", "AcceptanceCriterion"),
            ("Test", "Component"),
            ("Test", "Capability"),
        )),
    "contains": EdgeType(
        "contains", "TO is nested inside FROM", acyclic=True,
        describes="Structural nesting, not dependency.",
        pairs=(
            ("Container", "Component"),
            ("Module", "Module"),
        )),
}

# Layer crossings are therefore countable and few, which is the point of
# keeping the matrix small: business -> engineering happens only through
# `delivered_by` and `governs`; engineering -> technical only through
# `realized_in` and `contains`; business -> technical never, because a PRD does
# not name a directory. `verifies` is the single upward edge.

# Node/edge lifecycle. `status` is the durable state of a node in the repo
# graph, and is deliberately orthogonal to C4's NEW/MODIFIED/REMOVED/RELATED —
# those are a function of a changeset, computed by `graph diff`, never stored.
NODE_STATUSES = ("proposed", "active", "deprecated")

# Where a node or edge came from. `agent` is the one that earns scrutiny: the
# `no-evidence` advisory in graph.integrity fires only on agent-sourced records
# with an empty evidence list.
SOURCE_KINDS = ("agent", "detector", "human", "import")

# Below this, `graph validate` raises a low-confidence advisory. Same 0-100
# scale as a profile's meta.confidence_score, so the two read alike.
LOW_CONFIDENCE = 60


def layer_of(node_type: str) -> str:
    """The layer a node type belongs to.

    The single place layer is decided. Deriving it rather than storing it on
    the node is what removes the need for a rule policing `type` against a
    stored `layer` that could contradict it.
    """
    return NODE_TYPES[node_type].layer


def prefix_to_type() -> Dict[str, str]:
    """``{"cmp": "Component", ...}`` — the inverse of NodeType.id_prefix.

    Must be injective, or the `id-prefix-matches-type` integrity rule is
    unenforceable. A guard test asserts it.
    """
    return {spec.id_prefix: name for name, spec in NODE_TYPES.items()}


def edge_pair_allowed(from_type: str, edge_type: str, to_type: str) -> bool:
    """Whether the matrix permits this triple. Unknown types answer False —
    `unknown-type` is a separate finding, and reporting both for one edge would
    say the same thing twice."""
    spec = EDGE_TYPES.get(edge_type)
    if spec is None:
        return False
    return (from_type, to_type) in spec.pairs
