"""Tests for the ACSDD CLI's capability validator and catalog builder."""

import textwrap
from pathlib import Path

import pytest
import yaml

from acsdd.capability.loader import load_manifest, iter_manifests, ManifestLoadError
from acsdd.capability.validator import validate_manifest, validate_catalog
from acsdd.catalog.builder import build_catalog_markdown


VALID_MANIFEST = textwrap.dedent("""
    capability:
      id: "TEST-001"
      version: "1.0.0"
      name: "Test Capability"
      category: "DB"
      description: "A minimal valid capability for testing."
      inputs: []
      outputs:
        - name: "out"
          type: "string"
      adapters:
        - stack: "test-stack"
          adapter-id: "test-adapter"
""")


@pytest.fixture
def real_manifests_dir():
    return Path(__file__).parent.parent / ".acsdd" / "capabilities" / "_manifests"


def test_load_manifest(tmp_path):
    p = tmp_path / "TEST-001.yaml"
    p.write_text(VALID_MANIFEST)
    data = load_manifest(p)
    assert data["capability"]["id"] == "TEST-001"


def test_load_manifest_bad_yaml(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("capability: [unterminated")
    with pytest.raises(ManifestLoadError):
        load_manifest(p)


def test_validate_manifest_passes(tmp_path):
    p = tmp_path / "TEST-001.yaml"
    p.write_text(VALID_MANIFEST)
    data = load_manifest(p)
    result = validate_manifest(p, data)
    assert result.ok, result.errors


def test_validate_manifest_bad_id_pattern(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(VALID_MANIFEST.replace("TEST-001", "test-001"))
    data = load_manifest(p)
    result = validate_manifest(p, data)
    assert not result.ok
    assert any("id" in e for e in result.errors)


def test_validate_manifest_missing_required_field(tmp_path):
    p = tmp_path / "bad.yaml"
    data = yaml.safe_load(VALID_MANIFEST)
    del data["capability"]["outputs"]
    p.write_text(yaml.dump(data))
    loaded = load_manifest(p)
    result = validate_manifest(p, loaded)
    assert not result.ok


def test_validate_catalog_missing_dependency():
    manifests = {
        "AA-001": yaml.safe_load(VALID_MANIFEST.replace("TEST-001", "AA-001")),
    }
    manifests["AA-001"]["capability"]["dependencies"] = [{"capability": "ZZ-999"}]
    problems = validate_catalog(manifests)
    assert "AA-001" in problems
    assert any("ZZ-999" in msg for msg in problems["AA-001"])


def test_validate_catalog_circular_dependency():
    a = yaml.safe_load(VALID_MANIFEST.replace("TEST-001", "AA-001"))
    b = yaml.safe_load(VALID_MANIFEST.replace("TEST-001", "AA-002"))
    a["capability"]["dependencies"] = [{"capability": "AA-002"}]
    b["capability"]["dependencies"] = [{"capability": "AA-001"}]
    problems = validate_catalog({"AA-001": a, "AA-002": b})
    assert "AA-001" in problems and "AA-002" in problems
    assert any("circular" in msg for msg in problems["AA-001"])


def test_validate_catalog_self_dependency():
    a = yaml.safe_load(VALID_MANIFEST.replace("TEST-001", "AA-001"))
    a["capability"]["dependencies"] = [{"capability": "AA-001"}]
    problems = validate_catalog({"AA-001": a})
    assert "AA-001" in problems
    assert any("itself" in msg for msg in problems["AA-001"])


def test_validate_catalog_clean_graph_has_no_problems():
    a = yaml.safe_load(VALID_MANIFEST.replace("TEST-001", "AA-001"))
    b = yaml.safe_load(VALID_MANIFEST.replace("TEST-001", "AA-002"))
    b["capability"]["dependencies"] = [{"capability": "AA-001"}]
    problems = validate_catalog({"AA-001": a, "AA-002": b})
    assert problems == {}


# --- Tests against the real, shipped DB-00x manifests -----------------

def test_real_manifests_all_valid(real_manifests_dir):
    for path, data in iter_manifests(real_manifests_dir):
        result = validate_manifest(path, data)
        assert result.ok, f"{path}: {result.errors}"


def test_real_manifests_dependency_graph_is_clean(real_manifests_dir):
    manifests = {}
    for path, data in iter_manifests(real_manifests_dir):
        manifests[data["capability"]["id"]] = data
    problems = validate_catalog(manifests)
    assert problems == {}


def test_db_002_003_004_all_depend_on_db_001(real_manifests_dir):
    manifests = {}
    for path, data in iter_manifests(real_manifests_dir):
        manifests[data["capability"]["id"]] = data
    for cap_id in ("DB-002", "DB-003", "DB-004"):
        deps = [d["capability"] for d in manifests[cap_id]["capability"].get("dependencies", [])]
        assert deps == ["DB-001"], f"{cap_id} dependencies: {deps}"


# --- Catalog builder ----------------------------------------------------

def test_build_catalog_markdown_lists_all_capabilities(real_manifests_dir, tmp_path):
    manifests = {}
    for path, data in iter_manifests(real_manifests_dir):
        manifests[data["capability"]["id"]] = data

    md = build_catalog_markdown(
        manifests,
        docs_root=real_manifests_dir.parent,
        manifests_root=real_manifests_dir,
    )
    for cap_id in manifests:
        assert cap_id in md


def test_build_catalog_markdown_empty_categories_marked_none_yet():
    manifests = {"AA-001": yaml.safe_load(VALID_MANIFEST.replace("TEST-001", "AA-001"))}
    md = build_catalog_markdown(
        manifests, docs_root=Path("/tmp/nonexistent"), manifests_root=Path("/tmp/nonexistent/_manifests")
    )
    assert "*(none yet)*" in md
    assert "AA-001" in md
