"""Persistence: atomic writes, the revision log, and changeset round trips.

graph.json is the first large durable file acsdd rewrites rather than creates
or deletes, so the tests that matter most here are the failure ones — a
half-written graph is worse than any failure mode the tool had before.
"""

import json
from datetime import date

import pytest

from acsdd.graph.changeset import (
    ChangeSetError,
    GraphChangeSet,
    GraphOperation,
    apply_operations,
)
from acsdd.graph.model import Graph, GraphEdge, GraphLoadError, GraphNode, serialize
from acsdd.graph.repository import (
    ChangeRecord,
    ChangeStore,
    JsonGraphRepository,
    load_changeset,
    next_revision_id,
)

FIXED_DATE = date(2026, 8, 10)


def _graph_with(*names: str) -> Graph:
    graph = Graph()
    for name in names:
        graph.nodes[f"cmp:{name}"] = GraphNode(
            id=f"cmp:{name}", type="Component", name=name.title())
    return graph


# -- loading -------------------------------------------------------------

def test_loading_a_repo_with_no_graph_returns_an_empty_one(tmp_path):
    """The first `graph apply` in a repository has nothing to read. That is a
    starting state, not an error."""
    repo = JsonGraphRepository(tmp_path / "graph")

    assert repo.exists() is False
    assert repo.load().nodes == {}
    assert repo.revisions() == []
    assert repo.head() is None


def test_corrupt_json_raises_graph_load_error(tmp_path):
    graph_dir = tmp_path / "graph"
    graph_dir.mkdir()
    (graph_dir / "graph.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(GraphLoadError, match="not parseable JSON"):
        JsonGraphRepository(graph_dir).load()


def test_a_duplicate_id_on_disk_raises_rather_than_loading_silently(tmp_path):
    graph_dir = tmp_path / "graph"
    graph_dir.mkdir()
    (graph_dir / "graph.json").write_text(json.dumps({"graph": {
        "meta": {"version": "1.0.0", "generated_at": "2026-08-10"},
        "nodes": [{"id": "cmp:x", "type": "Component", "name": "One"},
                  {"id": "cmp:x", "type": "Component", "name": "Two"}],
        "edges": []}}), encoding="utf-8")

    with pytest.raises(GraphLoadError, match="duplicate node id"):
        JsonGraphRepository(graph_dir).load()


# -- saving --------------------------------------------------------------

def test_save_writes_the_graph_and_appends_a_revision(tmp_path):
    repo = JsonGraphRepository(tmp_path / "graph")
    graph = _graph_with("checkout")
    revision = repo.plan_revision(graph, "checkout-guest", "Guest checkout",
                                  applied_at=FIXED_DATE)

    repo.save(graph, revision, acsdd_version="0.10.0", generated_at=FIXED_DATE)

    assert repo.exists()
    assert repo.load().nodes.keys() == {"cmp:checkout"}
    assert [r.id for r in repo.revisions()] == [revision.id]
    assert repo.head().changeset_id == "checkout-guest"


def test_the_saved_graph_carries_its_revision_id(tmp_path):
    repo = JsonGraphRepository(tmp_path / "graph")
    graph = _graph_with("checkout")
    revision = repo.plan_revision(graph, None, "First", applied_at=FIXED_DATE)

    repo.save(graph, revision, generated_at=FIXED_DATE)

    assert repo.load().revision == revision.id


def test_revision_ids_are_sequence_plus_content_hash(tmp_path):
    repo = JsonGraphRepository(tmp_path / "graph")
    first = repo.plan_revision(_graph_with("a"), None, "First", applied_at=FIXED_DATE)
    repo.save(_graph_with("a"), first, generated_at=FIXED_DATE)
    second = repo.plan_revision(_graph_with("a", "b"), None, "Second",
                                applied_at=FIXED_DATE)

    assert first.id.startswith("0001-")
    assert second.id.startswith("0002-")
    assert second.parent == first.id


def test_the_same_content_hashes_the_same_across_repositories(tmp_path):
    """Content-addressed ids are what make two people applying the same
    changeset to the same base land on the same revision, so a divergence is
    detectable rather than silent."""
    left = JsonGraphRepository(tmp_path / "left")
    right = JsonGraphRepository(tmp_path / "right")

    a = left.plan_revision(_graph_with("x"), None, "T", applied_at=FIXED_DATE)
    b = right.plan_revision(_graph_with("x"), None, "T", applied_at=FIXED_DATE)

    assert a.id == b.id
    assert a.graph_hash == b.graph_hash


def test_saving_twice_is_a_clean_line_diff(tmp_path):
    """The property a reviewer depends on: adding one node changes the lines
    for that node and nothing else."""
    repo = JsonGraphRepository(tmp_path / "graph")
    repo.save(_graph_with("a"), repo.plan_revision(_graph_with("a"), None, "1",
                                                   applied_at=FIXED_DATE),
              generated_at=FIXED_DATE)
    before = repo.graph_path.read_text(encoding="utf-8").splitlines()

    bigger = _graph_with("a", "b")
    repo.save(bigger, repo.plan_revision(bigger, None, "2", applied_at=FIXED_DATE),
              generated_at=FIXED_DATE)
    after = repo.graph_path.read_text(encoding="utf-8").splitlines()

    # Everything that moved is either the new node's own lines, structural
    # punctuation around it, or the revision/hash in meta. Adding one node must
    # not reflow the rest of the file — that is what makes graph.json
    # reviewable once it has thousands of lines.
    added = [line.strip() for line in after if line not in before]
    unexplained = [line for line in added
                   if "cmp:b" not in line
                   and '"name": "B"' not in line
                   and "revision" not in line
                   and "graph_hash" not in line
                   and line not in {"{", "}", "},", "]", "["}]

    assert unexplained == [], unexplained
    assert len(added) < 10, added


# -- atomicity -----------------------------------------------------------

def test_a_failed_write_leaves_the_original_intact_and_no_temp_file(tmp_path, monkeypatch):
    """The failure that matters. A half-written graph.json is worse than any
    failure mode acsdd had before this subsystem."""
    repo = JsonGraphRepository(tmp_path / "graph")
    good = _graph_with("a")
    repo.save(good, repo.plan_revision(good, None, "1", applied_at=FIXED_DATE),
              generated_at=FIXED_DATE)
    original = repo.graph_path.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("acsdd.graph.repository.os.replace", boom)

    bigger = _graph_with("a", "b")
    with pytest.raises(OSError, match="disk full"):
        repo.save(bigger, repo.plan_revision(bigger, None, "2", applied_at=FIXED_DATE),
                  generated_at=FIXED_DATE)

    assert repo.graph_path.read_text(encoding="utf-8") == original
    leftovers = [p.name for p in repo.graph_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], leftovers


def test_writing_creates_missing_directories(tmp_path):
    repo = JsonGraphRepository(tmp_path / "deep" / "graph")
    graph = _graph_with("a")

    repo.save(graph, repo.plan_revision(graph, None, "1", applied_at=FIXED_DATE),
              generated_at=FIXED_DATE)

    assert repo.graph_path.is_file()
    assert repo.revision_index.is_file()


def test_next_revision_id_is_zero_padded():
    assert next_revision_id(7, "4f2a91c3deadbeef") == "0007-4f2a91c3"


# -- changesets ----------------------------------------------------------

def test_changeset_round_trips(tmp_path):
    changeset = GraphChangeSet(
        id="checkout-guest", title="Guest checkout", base_revision="0001-aaaaaaaa",
        operations=[
            GraphOperation(op="add_node", target_id="cmp:x",
                           node=GraphNode(id="cmp:x", type="Component", name="X")),
            GraphOperation(op="add_edge", target_id="cmp:x|depends_on|cmp:y",
                           edge=GraphEdge.create("cmp:x", "depends_on", "cmp:y")),
            GraphOperation(op="remove_node", target_id="cmp:old", reason="folded in"),
        ])
    path = tmp_path / "changeset.json"
    path.write_text(json.dumps(changeset.to_dict()), encoding="utf-8")

    restored = load_changeset(path)

    assert restored.id == changeset.id
    assert restored.base_revision == "0001-aaaaaaaa"
    assert [o.op for o in restored.operations] == ["add_node", "add_edge", "remove_node"]
    assert restored.operations[2].reason == "folded in"


def test_a_changeset_without_its_envelope_is_rejected():
    with pytest.raises(ChangeSetError, match="no top-level 'changeset' mapping"):
        GraphChangeSet.from_dict({"id": "x", "title": "y", "operations": []})


def test_an_unknown_operation_is_rejected_at_parse_time():
    with pytest.raises(ChangeSetError, match="unknown operation 'rename_node'"):
        GraphChangeSet.from_dict({"changeset": {
            "id": "x", "title": "Title", "operations": [{"op": "rename_node"}]}})


def test_an_add_node_without_a_node_is_rejected():
    with pytest.raises(ChangeSetError, match="add_node without a 'node'"):
        GraphChangeSet.from_dict({"changeset": {
            "id": "x", "title": "Title", "operations": [{"op": "add_node"}]}})


def test_a_remove_without_an_id_is_rejected():
    with pytest.raises(ChangeSetError, match="remove_node without an 'id'"):
        GraphChangeSet.from_dict({"changeset": {
            "id": "x", "title": "Title", "operations": [{"op": "remove_node"}]}})


# -- the change store ----------------------------------------------------

def test_change_record_round_trips(tmp_path):
    store = ChangeStore(tmp_path / "changes")
    record = ChangeRecord(id="checkout-guest", title="Guest checkout",
                          created_at="2026-08-10", prd_path="docs/prd/checkout.md")

    store.save_record(record)

    assert store.exists("checkout-guest")
    assert store.load_record("checkout-guest") == record


def test_open_ids_excludes_applied_changes(tmp_path):
    """What auto-detection keys off: an applied change is history, and writing
    a new changeset into one is almost never the intent."""
    store = ChangeStore(tmp_path / "changes")
    for cid in ("one", "two"):
        store.save_record(ChangeRecord(id=cid, title=cid, created_at="2026-08-10"))
    store.mark_applied("one", "0001-aaaaaaaa", applied_at=FIXED_DATE)

    assert store.list_ids() == ["one", "two"]
    assert store.open_ids() == ["two"]
    assert store.applied_revision("one") == "0001-aaaaaaaa"
    assert store.applied_revision("two") is None


def test_a_directory_without_a_change_record_is_not_a_change(tmp_path):
    changes = tmp_path / "changes"
    (changes / "stray").mkdir(parents=True)

    assert ChangeStore(changes).list_ids() == []


def test_saved_changeset_reloads_through_the_store(tmp_path):
    store = ChangeStore(tmp_path / "changes")
    changeset = GraphChangeSet(id="c", title="Title", operations=[
        GraphOperation(op="add_node", target_id="cmp:x",
                       node=GraphNode(id="cmp:x", type="Component", name="X"))])

    store.save_changeset(changeset)

    assert store.load_changeset("c").operations[0].node.name == "X"


# -- repository + changeset together -------------------------------------

def test_apply_then_save_then_reload_preserves_the_result(tmp_path):
    repo = JsonGraphRepository(tmp_path / "graph")
    changeset = GraphChangeSet(id="c", title="Title", operations=[
        GraphOperation(op="add_node", target_id="cmp:x",
                       node=GraphNode(id="cmp:x", type="Component", name="X")),
        GraphOperation(op="add_edge", target_id="cmp:x|depends_on|cmp:y",
                       edge=GraphEdge.create("cmp:x", "depends_on", "cmp:y")),
    ])

    outcome = apply_operations(repo.load(), changeset)
    revision = repo.plan_revision(outcome.graph, changeset.id, changeset.title,
                                  applied_at=FIXED_DATE)
    repo.save(outcome.graph, revision, generated_at=FIXED_DATE)

    reloaded = repo.load()
    assert reloaded.nodes.keys() == {"cmp:x"}
    assert reloaded.edges.keys() == {"cmp:x|depends_on|cmp:y"}
    assert serialize(reloaded, generated_at=FIXED_DATE) == \
        repo.graph_path.read_text(encoding="utf-8")
