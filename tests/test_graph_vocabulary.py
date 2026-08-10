"""Guard tests for the graph vocabulary.

These are guards rather than coverage. The vocabulary is a permanent API — node
ids embed their type prefix and land in consumers' checked-in graph.json — and
it is mirrored in two JSON Schema files that cannot $ref each other. Nothing
here should be weakened to make a change pass; if one fails, the table and the
schema have drifted and one of them is wrong.
"""

import json
import re
from pathlib import Path

import pytest

from acsdd.catalog.builder import CATEGORY_ORDER
from acsdd.graph.vocabulary import (
    EDGE_TYPES,
    LAYERS,
    LOW_CONFIDENCE,
    NODE_ID_PATTERN,
    NODE_STATUSES,
    NODE_TYPES,
    SOURCE_KINDS,
    edge_pair_allowed,
    layer_of,
    prefix_to_type,
)

SCHEMA_DIR = Path(__file__).parent.parent / "src" / "acsdd" / "schemas"


def _schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def graph_schema():
    return _schema("engineering-graph.schema.json")


@pytest.fixture
def changeset_schema():
    return _schema("graph-changeset.schema.json")


def _def_enum(schema: dict, defname: str, prop: str) -> list:
    """Reach into a local $def's property enum.

    The graph schemas are the only ones in the repo using $defs (node, edge,
    evidence and source each repeat within a file), so the guard tests reach
    one level deeper than their capability/profile siblings. This helper is
    what keeps that from being spelled out at every assertion.
    """
    return schema["$defs"][defname]["properties"][prop]["enum"]


def test_node_type_enum_matches_the_json_schema(graph_schema):
    """The table and the schema must agree in both directions. A type in the
    table but not the schema is a node acsdd can build and then refuse to load;
    a type in the schema but not the table crashes layer_of()."""
    assert sorted(NODE_TYPES) == sorted(_def_enum(graph_schema, "node", "type"))


def test_edge_type_enum_matches_both_json_schemas(graph_schema, changeset_schema):
    """Asserted against both files. The two schemas duplicate node/edge rather
    than $ref-ing across files (which would need $id declarations and a
    resolver registry), so this is the only thing stopping them drifting — and
    a changeset that can express an edge the graph cannot hold is a changeset
    that passes validation and fails at apply."""
    expected = sorted(EDGE_TYPES)
    assert sorted(_def_enum(graph_schema, "edge", "type")) == expected
    assert sorted(_def_enum(changeset_schema, "edge", "type")) == expected


def test_node_type_enum_matches_between_both_json_schemas(graph_schema, changeset_schema):
    assert (sorted(_def_enum(graph_schema, "node", "type"))
            == sorted(_def_enum(changeset_schema, "node", "type")))


def test_id_pattern_matches_the_json_schema(graph_schema, changeset_schema):
    """Byte-identical, not merely equivalent. Two regexes that mean the same
    thing today diverge the first time one is edited."""
    for schema in (graph_schema, changeset_schema):
        assert schema["$defs"]["node"]["properties"]["id"]["pattern"] == NODE_ID_PATTERN
        for endpoint in ("from", "to"):
            assert (schema["$defs"]["edge"]["properties"][endpoint]["pattern"]
                    == NODE_ID_PATTERN)


def test_status_and_source_kind_enums_match_the_json_schema(graph_schema):
    assert sorted(_def_enum(graph_schema, "node", "status")) == sorted(NODE_STATUSES)
    assert sorted(_def_enum(graph_schema, "source", "kind")) == sorted(SOURCE_KINDS)


def test_every_node_type_appears_in_the_edge_matrix():
    """A node type nothing can connect to is a dead type and a graph nobody can
    build. This is what stops the vocabulary growing by accretion."""
    connected = set()
    for spec in EDGE_TYPES.values():
        for from_type, to_type in spec.pairs:
            connected.add(from_type)
            connected.add(to_type)

    orphans = sorted(set(NODE_TYPES) - connected)
    assert not orphans, (
        f"{orphans} appear in NODE_TYPES but in no edge pair — nothing can ever "
        f"link to them, so no graph can contain them meaningfully")


def test_every_edge_pair_names_a_real_node_type():
    for name, spec in EDGE_TYPES.items():
        for from_type, to_type in spec.pairs:
            assert from_type in NODE_TYPES, f"{name}: unknown from-type {from_type}"
            assert to_type in NODE_TYPES, f"{name}: unknown to-type {to_type}"


def test_id_prefixes_are_unique_across_node_types():
    """The `id-prefix-matches-type` integrity rule is unenforceable without an
    injective prefix map — two types sharing 'cmp' means an id says nothing."""
    prefixes = [spec.id_prefix for spec in NODE_TYPES.values()]
    assert len(prefixes) == len(set(prefixes)), sorted(prefixes)
    assert len(prefix_to_type()) == len(NODE_TYPES)


def test_every_id_prefix_can_start_a_legal_node_id():
    """A prefix the id pattern rejects would make every node of that type
    unrepresentable — caught here rather than at the first import."""
    pattern = re.compile(NODE_ID_PATTERN)
    for name, spec in NODE_TYPES.items():
        assert pattern.match(f"{spec.id_prefix}:example-node"), name


def test_every_node_type_declares_a_known_layer():
    for name, spec in NODE_TYPES.items():
        assert spec.layer in LAYERS, name
        assert layer_of(name) == spec.layer


def test_business_types_are_never_durable_and_the_rest_always_are():
    """The repo-graph / change-overlay split. A durable Requirement would
    accumulate satisfied requirements in graph.json forever; a non-durable
    Component would be rediscovered from scratch on every change."""
    for name, spec in NODE_TYPES.items():
        assert spec.durable == (spec.layer != "business"), name


def test_business_layer_never_reaches_the_technical_layer():
    """A PRD does not name a directory. Business nodes reach the technical
    layer only through an engineering node, and that indirection is what makes
    Requirement -> Capability -> Component -> Module traceable rather than
    asserted."""
    for name, spec in EDGE_TYPES.items():
        for from_type, to_type in spec.pairs:
            crossing = (layer_of(from_type), layer_of(to_type))
            assert crossing != ("business", "technical"), f"{name}: {from_type}->{to_type}"


def test_edge_readings_and_descriptions_are_present():
    """`reading` is published in the `graph context --json` payload so a skill
    never has to guess an edge's direction. An empty one is a silent gap."""
    for name, spec in EDGE_TYPES.items():
        assert spec.reading, name
        assert "FROM" in spec.reading and "TO" in spec.reading, name
        assert spec.pairs, f"{name} allows no pairs at all"


def test_node_types_describe_themselves():
    for name, spec in NODE_TYPES.items():
        assert spec.describes.endswith("."), name
        assert set(spec.required_attributes) <= set(spec.attributes), name


def test_capability_category_attribute_matches_the_capability_schema():
    """The Capability node type carries the manifest's category as an
    attribute. Its values have to stay inside the capability schema's enum, or
    a graph could name a category no manifest can declare."""
    capability_schema = _schema("capability.schema.json")
    enum = capability_schema["properties"]["capability"]["properties"]["category"]["enum"]

    assert sorted(enum) == sorted(CATEGORY_ORDER)
    assert "category" in NODE_TYPES["Capability"].required_attributes


def test_edge_pair_allowed_answers_the_matrix():
    assert edge_pair_allowed("Requirement", "delivered_by", "Capability")
    assert not edge_pair_allowed("Requirement", "delivered_by", "Component")
    assert not edge_pair_allowed("Requirement", "realized_in", "Module")
    # An unknown edge type answers False rather than raising: `unknown-type` is
    # a separate finding, and reporting both would say the same thing twice.
    assert not edge_pair_allowed("Requirement", "invented", "Capability")


def test_low_confidence_threshold_is_on_the_schema_scale():
    graph_schema = _schema("engineering-graph.schema.json")
    confidence = graph_schema["$defs"]["node"]["properties"]["confidence"]
    assert confidence["minimum"] <= LOW_CONFIDENCE <= confidence["maximum"]
