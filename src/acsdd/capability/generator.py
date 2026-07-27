"""Scaffolds a new draft capability manifest from an engineering profile.

Mirrors PROFILE-001's own discovery pattern: everything derivable from
already-known data (the profile produced by `acsdd profile discover`) is
filled in automatically; everything that requires actual knowledge of what
this specific capability does (its name, description, concrete
inputs/outputs, and adapter id) is left as an explicit [REVIEW REQUIRED]
placeholder for a human — or an AI coding agent reading the target
repository — to complete.
"""

from typing import Dict, List, Optional

REVIEW_PLACEHOLDER = "[REVIEW REQUIRED]"


def scaffold_manifest(profile: Dict, cap_id: str, category: str,
                       name: Optional[str] = None) -> Dict:
    """profile: the ``profile:`` sub-dict of a profile YAML (draft or
    completed). Returns a dict matching capability.schema.json's shape."""
    stack_info = profile.get("technology_stack", {}) or {}
    cap_config = profile.get("capability_configuration", {}) or {}
    quality_gates = profile.get("quality_gates", {}) or {}

    default_adapter = cap_config.get("default_adapter")
    if not default_adapter or str(default_adapter).startswith("[REVIEW REQUIRED"):
        default_adapter = REVIEW_PLACEHOLDER

    profile_constraints: List[str] = []
    for field in ("language", "framework", "version", "orm", "database"):
        value = stack_info.get(field)
        if value and not str(value).startswith("[REVIEW REQUIRED"):
            profile_constraints.append(f"{field}:{value}")

    return {
        "capability": {
            "id": cap_id,
            "version": "0.1.0",
            "name": name or f"{REVIEW_PLACEHOLDER} — name this capability",
            "category": category,
            "description": (
                f"{REVIEW_PLACEHOLDER} — describe what this capability does "
                "and why an AI agent would invoke it."
            ),
            "inputs": [],
            "outputs": [
                {"name": REVIEW_PLACEHOLDER, "type": "file-path", "pattern": REVIEW_PLACEHOLDER},
            ],
            "dependencies": [],
            "adapters": [
                {"stack": default_adapter, "adapter-id": f"{cap_id}-{REVIEW_PLACEHOLDER}"},
            ],
            "profile_constraints": profile_constraints,
            "quality_gates": list(quality_gates.get("automatic", []) or []),
            "token_budget": {"planning": 4000, "implementation": 8000, "validation": 2000},
        }
    }
