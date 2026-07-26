"""Validates capability manifests against the Appendix A JSON Schema, plus
checks that go beyond what JSON Schema alone can express: dependency
references pointing at capabilities that actually exist, and no circular
dependency chains.
"""

import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Dict, List

import jsonschema


@dataclass
class ValidationResult:
    path: Path
    capability_id: str | None
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _load_schema() -> Dict:
    schema_text = resources.files("acsdd.schemas").joinpath(
        "capability.schema.json"
    ).read_text(encoding="utf-8")
    return json.loads(schema_text)


_SCHEMA = None


def get_schema() -> Dict:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = _load_schema()
    return _SCHEMA


def validate_manifest(path: Path, data: Dict) -> ValidationResult:
    """Schema-only validation of a single already-loaded manifest dict."""
    cap_id = None
    try:
        cap_id = data.get("capability", {}).get("id")
    except AttributeError:
        pass

    result = ValidationResult(path=path, capability_id=cap_id)
    validator = jsonschema.Draft202012Validator(get_schema())
    for err in sorted(validator.iter_errors(data), key=lambda e: e.path):
        loc = "/".join(str(p) for p in err.path) or "<root>"
        result.errors.append(f"{loc}: {err.message}")
    return result


def validate_catalog(manifests: Dict[str, Dict]) -> Dict[str, List[str]]:
    """Cross-manifest checks across an already-loaded {capability_id: data}
    map: schema validity was already checked per-file; this checks the
    relationships between them.

    Returns {capability_id: [error, ...]} — only entries with errors.
    """
    problems: Dict[str, List[str]] = {}

    def add(cap_id: str, msg: str):
        problems.setdefault(cap_id, []).append(msg)

    for cap_id, data in manifests.items():
        cap = data.get("capability", {})
        declared_id = cap.get("id")
        if declared_id and declared_id != cap_id:
            add(cap_id, f"filename/id mismatch: manifest declares id '{declared_id}'")

        for dep in cap.get("dependencies", []) or []:
            dep_id = dep.get("capability")
            if dep_id and dep_id not in manifests:
                add(cap_id, f"depends on '{dep_id}', which has no manifest in this set")
            if dep_id == cap_id:
                add(cap_id, "depends on itself")

    # Circular dependency detection (simple DFS over the whole set).
    def has_cycle(start: str) -> List[str] | None:
        stack = [(start, [start])]
        while stack:
            node, path = stack.pop()
            deps = manifests.get(node, {}).get("capability", {}).get("dependencies", []) or []
            for dep in deps:
                dep_id = dep.get("capability")
                if not dep_id or dep_id not in manifests:
                    continue
                if dep_id == start:
                    return path + [dep_id]
                if dep_id not in path:
                    stack.append((dep_id, path + [dep_id]))
        return None

    for cap_id in manifests:
        cycle = has_cycle(cap_id)
        if cycle:
            add(cap_id, f"circular dependency: {' -> '.join(cycle)}")

    return problems
