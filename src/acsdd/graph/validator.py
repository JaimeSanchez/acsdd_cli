"""JSON Schema validation for graph documents and changesets.

The same charter `capability.validator.validate_manifest` has: *shape only*.
Whether an edge may legally join two node types, whether every Requirement
reaches a Capability, whether the graph has a cycle — none of that is here.
It is in `graph.integrity`, which is this module's `validate_catalog`.

`GraphValidationResult` is a third near-copy of the `ValidationResult`
dataclass in `capability/validator.py` and `profile/validator.py`. That is
deliberate for now: extracting a shared `acsdd.validation` would touch two
stable, tested modules for zero behaviour change. Three copies is where the
argument starts to flip — **if a fourth appears, extract it then.**
"""

import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Dict, List, Optional

import jsonschema


@dataclass
class GraphValidationResult:
    path: Optional[Path]
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _load_schema(filename: str) -> Dict:
    schema_text = resources.files("acsdd.schemas").joinpath(
        filename
    ).read_text(encoding="utf-8")
    return json.loads(schema_text)


_GRAPH_SCHEMA = None
_CHANGESET_SCHEMA = None


def get_graph_schema() -> Dict:
    global _GRAPH_SCHEMA
    if _GRAPH_SCHEMA is None:
        _GRAPH_SCHEMA = _load_schema("engineering-graph.schema.json")
    return _GRAPH_SCHEMA


def get_changeset_schema() -> Dict:
    global _CHANGESET_SCHEMA
    if _CHANGESET_SCHEMA is None:
        _CHANGESET_SCHEMA = _load_schema("graph-changeset.schema.json")
    return _CHANGESET_SCHEMA


def _validate(data: Dict, schema: Dict, path: Optional[Path]) -> GraphValidationResult:
    result = GraphValidationResult(path=path)
    validator = jsonschema.Draft202012Validator(schema)
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "<root>"
        result.errors.append(f"{loc}: {err.message}")
    return result


def validate_graph_document(data: Dict,
                            path: Optional[Path] = None) -> GraphValidationResult:
    """Schema-only validation of an already-loaded graph document."""
    return _validate(data, get_graph_schema(), path)


def validate_changeset_document(data: Dict,
                                path: Optional[Path] = None) -> GraphValidationResult:
    """Schema-only validation of an already-loaded changeset document."""
    return _validate(data, get_changeset_schema(), path)
