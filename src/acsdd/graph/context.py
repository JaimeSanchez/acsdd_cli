"""The `acsdd graph context --json` payload — the contract an agent writes against.

This module is the whole reason acsdd needs no LLM of its own. Interpreting a
PRD is judgement work and belongs to an agent; what belongs to acsdd is
publishing exactly what the agent must know to produce a changeset that will
pass the gate, and then being the gate. Every key here exists so the packaged
`graph-import` SKILL.md never has to restate a fact Python already holds —
`vocabulary.edge_types[].pairs` *is* the allowed-edge matrix, and `rules` *is*
the integrity table, both serialized straight out of their source of truth.

That is why `tests/test_graph_context.py` asserts the payload advertises every
node type, every edge type and every rule. A rule an agent never learns about
is a rule that fires as a surprise at apply time, which is exactly the
experience the payload exists to prevent. It is the same guard as
`test_review_covers_every_placeholder_real_discovery_emits`.

`capability.recommender`'s construction and constraints: static tables plus
dict probes. No detector is re-run, nothing walks the repository, and the
profile and manifests arrive as already-loaded dicts.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

from acsdd.graph import vocabulary
from acsdd.graph.changeset import OPERATIONS
from acsdd.graph.integrity import RULES
from acsdd.graph.model import Graph

# How many nodes a payload will carry before it truncates. An unbounded
# subgraph is an unbounded prompt; 400 nodes is roughly the point past which a
# reader — human or model — stops treating it as context and starts treating it
# as noise.
DEFAULT_MAX_NODES = 400

# What each purpose needs to see. A PRD import needs to know what exists to
# attach requirements to, not the module tree; a repo map is the opposite.
PURPOSES: Dict[str, Tuple[str, ...]] = {
    "prd-import": ("engineering",),
    "repo-map": ("engineering", "technical"),
    "spec-check": ("business", "engineering", "technical"),
}
DEFAULT_PURPOSE = "prd-import"


@dataclass
class ContextPayload:
    """The serialized context. Built by `build_context`, emitted by `cli.py`."""

    purpose: str
    generated_at: str
    graph_path: str
    graph_revision: Optional[str]
    change: Optional[Dict] = None
    profile: Optional[Dict] = None
    capabilities: List[Dict] = field(default_factory=list)
    subgraph: Dict = field(default_factory=dict)
    counts: Dict = field(default_factory=dict)
    changeset_format: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "purpose": self.purpose,
            "generated_at": self.generated_at,
            "graph_path": self.graph_path,
            "graph_revision": self.graph_revision,
            "change": self.change,
            "vocabulary": vocabulary_payload(),
            "rules": rules_payload(),
            "changeset_format": self.changeset_format,
            "profile": self.profile,
            "capabilities": self.capabilities,
            "subgraph": self.subgraph,
            "counts": self.counts,
        }


def vocabulary_payload() -> Dict:
    """The closed vocabulary, serialized from `graph.vocabulary`.

    `edge_types[].pairs` is the allowed-edge matrix. The skill reads it rather
    than restating it, which is what stops the two drifting the first time a
    node type is added.
    """
    return {
        "layers": list(vocabulary.LAYERS),
        "id_pattern": vocabulary.NODE_ID_PATTERN,
        "node_types": [
            {
                "type": name,
                "layer": spec.layer,
                "id_prefix": spec.id_prefix,
                "durable": spec.durable,
                "describes": spec.describes,
                "attributes": list(spec.attributes),
                "required_attributes": list(spec.required_attributes),
            }
            for name, spec in vocabulary.NODE_TYPES.items()
        ],
        "edge_types": [
            {
                "type": name,
                "reading": spec.reading,
                "describes": spec.describes,
                "acyclic": spec.acyclic,
                "pairs": [list(pair) for pair in spec.pairs],
            }
            for name, spec in vocabulary.EDGE_TYPES.items()
        ],
        "source_kinds": list(vocabulary.SOURCE_KINDS),
        "node_status": list(vocabulary.NODE_STATUSES),
        "confidence": {"min": 0, "max": 100,
                       "low_threshold": vocabulary.LOW_CONFIDENCE},
    }


def rules_payload() -> List[Dict]:
    """Every integrity rule and what it costs to break.

    Published so an agent knows the stakes before it writes, rather than
    discovering them when `graph apply` refuses.
    """
    return [rule.to_dict() for rule in RULES]


def changeset_format_payload(change_id: Optional[str], changeset_path: Optional[str],
                             base_revision: Optional[str]) -> Dict:
    """How to write the changeset, including the exact commands to verify it."""
    # The example is written to be a *valid* document, not a template full of
    # angle brackets: an agent copies it and edits it down, and a skeleton that
    # fails the schema on its own teaches the wrong shape. Only free-text
    # fields carry placeholders; every id, date and enum is real.
    slug = change_id or "example-change"
    requirement_id = f"req:{slug}.pay-without-account"
    example = {
        "changeset": {
            "id": slug,
            "title": "One line describing the change",
            "base_revision": base_revision,
            "source": {"kind": "agent", "ref": "docs/prd/example.md",
                       "at": "2026-01-31", "agent": "your-tool/graph-import"},
            "operations": [
                {
                    "op": "add_node",
                    "node": {
                        "id": requirement_id,
                        "type": "Requirement",
                        "name": "What the stakeholder asked for",
                        "summary": "One or two sentences, in the PRD's own terms.",
                        "attributes": {"actor": "shopper", "priority": "must"},
                        "evidence": [{"path": "docs/prd/example.md", "lines": "12-18",
                                      "quote": "The sentence this came from, verbatim."}],
                        "confidence": 90,
                    },
                    "reason": "stated in the PRD",
                },
                {
                    "op": "add_edge",
                    "edge": {
                        "type": "delivered_by",
                        "from": requirement_id,
                        "to": "cap:BE-001",
                        "rationale": "Why this capability is what delivers it.",
                        "confidence": 80,
                    },
                },
            ],
        }
    }

    path = changeset_path or ".acsdd/changes/<change-id>/changeset.json"
    return {
        "envelope": "changeset",
        "operations": list(OPERATIONS),
        "base_revision": base_revision,
        "edge_id_rule": ("edge ids are derived as '<from>|<type>|<to>' — do not "
                         "author them; supply type, from and to only"),
        "business_id_rule": (
            f"business-layer node ids must start with '{change_id}.' after the "
            f"prefix, e.g. req:{change_id}.pay-without-account"
            if change_id else
            "business-layer node ids must be namespaced by their change id"),
        "no_update_edge": ("there is no update_edge: an edge IS (from, type, to), "
                           "so changing its metadata is remove_edge then add_edge"),
        "write_to": path,
        "example": example,
        "dry_run_command": f"acsdd graph apply {path} --dry-run --json",
        "apply_command": f"acsdd graph apply {path} --json",
        "validate_command": (f"acsdd graph validate --change {change_id} --json"
                             if change_id else "acsdd graph validate --json"),
    }


def profile_payload(profile: Optional[Dict], profile_path: Optional[str]) -> Optional[Dict]:
    """The agreed facts about this repository, or None when there are none.

    `usable` is false for a draft, mirroring what the capability-plan skill
    already says out loud: recommendations derived from `[REVIEW REQUIRED]`
    placeholders are recommendations derived from nothing.
    """
    if not profile:
        return None

    from acsdd.profile.generator import find_unresolved_fields

    meta = profile.get("meta") or {}
    unresolved = find_unresolved_fields(profile)
    status = meta.get("status", "draft")

    return {
        "path": profile_path,
        "id": meta.get("id"),
        "status": status,
        "usable": status == "active" and not unresolved,
        "unresolved_field_count": len(unresolved),
        "technology_stack": profile.get("technology_stack") or {},
        "engineering_standards": profile.get("engineering_standards") or {},
        "quality_gates": profile.get("quality_gates") or {},
        "security_profile": profile.get("security_profile") or {},
    }


def capabilities_payload(manifests: Dict[str, Dict], graph: Graph) -> List[Dict]:
    """What an agent may attach a Requirement to.

    Read from `iter_manifests` output — the graph subsystem never re-parses the
    catalog itself. `in_graph` is what tells the agent whether it must create
    the Capability node or merely reference one.
    """
    payload = []
    for cap_id in sorted(manifests):
        capability = (manifests[cap_id].get("capability") or {})
        node_id = f"cap:{cap_id}"
        payload.append({
            "id": cap_id,
            "node_id": node_id,
            "in_graph": node_id in graph.nodes,
            "name": capability.get("name", ""),
            "category": capability.get("category", ""),
            "description": (capability.get("description") or "").strip(),
            "dependencies": [d.get("capability") for d
                             in (capability.get("dependencies") or [])
                             if d.get("capability")],
            "quality_gates": list(capability.get("quality_gates") or []),
        })
    return payload


def select_subgraph(graph: Graph, layers: Tuple[str, ...],
                    max_nodes: int = DEFAULT_MAX_NODES) -> Dict:
    """The slice of the graph this purpose needs, bounded.

    Truncation is by **layer priority**, never arbitrary: the layers are kept
    in the order the purpose lists them, so dropping happens from the least
    relevant end, and `truncated` says so rather than letting a short answer
    look complete.
    """
    selected = []
    for layer in layers:
        for node in sorted(graph.nodes.values(), key=lambda n: n.id):
            if node.type not in vocabulary.NODE_TYPES:
                continue
            if vocabulary.layer_of(node.type) == layer:
                selected.append(node)

    truncated = len(selected) > max_nodes
    kept = selected[:max_nodes]
    kept_ids = {n.id for n in kept}
    edges = [e for e in sorted(graph.edges.values(), key=lambda e: e.id)
             if e.from_id in kept_ids and e.to_id in kept_ids]

    return {
        "selected_by": "layer:" + "+".join(layers),
        "nodes": [n.to_dict() for n in kept],
        "edges": [e.to_dict() for e in edges],
        "node_count": len(kept),
        "edge_count": len(edges),
        "truncated": truncated,
        "max_nodes": max_nodes,
    }


def graph_counts(graph: Graph) -> Dict:
    by_type: Dict[str, int] = {}
    by_layer: Dict[str, int] = {}
    for node in graph.nodes.values():
        by_type[node.type] = by_type.get(node.type, 0) + 1
        if node.type in vocabulary.NODE_TYPES:
            layer = vocabulary.layer_of(node.type)
            by_layer[layer] = by_layer.get(layer, 0) + 1

    by_edge_type: Dict[str, int] = {}
    for edge in graph.edges.values():
        by_edge_type[edge.type] = by_edge_type.get(edge.type, 0) + 1

    return {
        "nodes": len(graph.nodes),
        "edges": len(graph.edges),
        "by_type": {k: by_type[k] for k in sorted(by_type)},
        "by_layer": {k: by_layer[k] for k in sorted(by_layer)},
        "by_edge_type": {k: by_edge_type[k] for k in sorted(by_edge_type)},
    }


def build_context(graph: Graph, graph_path: str, purpose: str = DEFAULT_PURPOSE,
                  change: Optional[Dict] = None, profile: Optional[Dict] = None,
                  profile_path: Optional[str] = None,
                  manifests: Optional[Dict[str, Dict]] = None,
                  max_nodes: int = DEFAULT_MAX_NODES,
                  generated_at: Optional[date] = None) -> ContextPayload:
    """Assemble the whole payload. Pure — everything it needs is an argument."""
    layers = PURPOSES.get(purpose, PURPOSES[DEFAULT_PURPOSE])
    change_id = (change or {}).get("id")

    return ContextPayload(
        purpose=purpose,
        generated_at=(generated_at or datetime.now(timezone.utc).date()).isoformat(),
        graph_path=graph_path,
        graph_revision=graph.revision,
        change=change,
        profile=profile_payload(profile, profile_path),
        capabilities=capabilities_payload(manifests or {}, graph),
        subgraph=select_subgraph(graph, layers, max_nodes=max_nodes),
        counts=graph_counts(graph),
        changeset_format=changeset_format_payload(
            change_id, (change or {}).get("changeset_path"), graph.revision),
    )
