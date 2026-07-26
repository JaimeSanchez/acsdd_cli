"""Loads capability manifest YAML files from a directory or single path."""

from pathlib import Path
from typing import Dict, Iterator

import yaml


class ManifestLoadError(Exception):
    """Raised when a manifest file can't be parsed at all (bad YAML, etc.)."""


def load_manifest(path: Path) -> Dict:
    """Load a single manifest file and return its raw dict (not yet
    schema-validated — see acsdd.capability.validator for that)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ManifestLoadError(f"{path}: invalid YAML — {e}") from e

    if not isinstance(data, dict):
        raise ManifestLoadError(f"{path}: top-level content is not a mapping")

    return data


def iter_manifests(manifests_dir: Path) -> Iterator[tuple[Path, Dict]]:
    """Yield (path, raw_dict) for every *.yaml / *.yml file in a directory,
    sorted by filename for deterministic output."""
    paths = sorted(
        p for p in manifests_dir.glob("*.y*ml")
        if p.is_file()
    )
    for path in paths:
        yield path, load_manifest(path)
