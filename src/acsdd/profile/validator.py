"""Validates an ACSDD Engineering Profile YAML file against the Appendix B
JSON Schema."""

import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Dict, List

import jsonschema
import yaml


@dataclass
class ProfileValidationResult:
    path: Path
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


_SCHEMA = None


def get_schema() -> Dict:
    global _SCHEMA
    if _SCHEMA is None:
        text = resources.files("acsdd.schemas").joinpath(
            "profile.schema.json"
        ).read_text(encoding="utf-8")
        _SCHEMA = json.loads(text)
    return _SCHEMA


def validate_profile_file(path: Path) -> ProfileValidationResult:
    result = ProfileValidationResult(path=path)
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        result.errors.append(f"invalid YAML — {e}")
        return result

    if not isinstance(data, dict):
        result.errors.append("top-level content is not a mapping")
        return result

    validator = jsonschema.Draft202012Validator(get_schema())
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "<root>"
        result.errors.append(f"{loc}: {err.message}")

    return result
