"""CLI-level tests for the `graph` and `change` groups.

The `change new` / `change remove` round trip lives here rather than in a file
of its own, following the convention that a remove's tests sit with its create
counterpart — the useful test is the round trip. Every no-`--force` test
asserts the files are **still on disk**, not merely that the exit code was 1: a
command that prints the refusal *and* deletes would pass the weaker assertion.
"""

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from acsdd.cli import cli

REAL_MANIFESTS = Path(__file__).parent.parent / ".acsdd" / "capabilities" / "_manifests"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo with the shipped example manifests and nothing else."""
    manifests = tmp_path / ".acsdd" / "capabilities" / "_manifests"
    manifests.mkdir(parents=True)
    for manifest in REAL_MANIFESTS.glob("*.yaml"):
        shutil.copy(manifest, manifests / manifest.name)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(*args, **kwargs):
    return CliRunner().invoke(cli, list(args), **kwargs)


def _changeset(repo: Path, change_id: str, operations: list,
               base_revision=None) -> Path:
    path = repo / ".acsdd" / "changes" / change_id / "changeset.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"changeset": {
        "id": change_id, "title": "A change", "base_revision": base_revision,
        "operations": operations}}), encoding="utf-8")
    return path


def _capability(cap_id: str, category: str = "DB") -> dict:
    return {"op": "add_node", "node": {
        "id": f"cap:{cap_id}", "type": "Capability", "name": f"Capability {cap_id}",
        "attributes": {"category": category}}}


def _component(slug: str) -> dict:
    return {"op": "add_node", "node": {
        "id": f"cmp:{slug}", "type": "Component", "name": slug.title()}}


def _edge(from_id: str, edge_type: str, to_id: str) -> dict:
    return {"op": "add_edge", "edge": {"type": edge_type, "from": from_id, "to": to_id}}


def _minimal_ops() -> list:
    return [_capability("DB-001"), _component("entities"),
            _edge("cap:DB-001", "implemented_by", "cmp:entities")]


# -- graph show ----------------------------------------------------------

def test_show_on_an_empty_repo_exits_zero(repo):
    """Informational commands report the empty state rather than failing —
    the first run in any repository hits this."""
    result = _run("graph", "show")

    assert result.exit_code == 0, result.output
    assert "0 nodes, 0 edges" in result.output
    assert not (repo / ".acsdd" / "graph").exists(), \
        "a read-only command must not conjure a graph directory"


def test_show_json_is_parseable_on_an_empty_repo(repo):
    result = _run("graph", "show", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["nodes"] == [] and payload["revision"] is None


def test_show_reports_counts_by_layer_after_an_apply(repo):
    _changeset(repo, "demo", _minimal_ops())
    _run("graph", "apply", "--change", "demo")

    result = _run("graph", "show")

    assert result.exit_code == 0, result.output
    assert "engineering (2)" in result.output
    assert "1  implemented_by" in result.output


def test_show_node_renders_the_neighbourhood_with_edge_types(repo):
    """The typed multi-edge render: a reader sees *how* two nodes relate, not
    only that they do."""
    _changeset(repo, "demo", _minimal_ops())
    _run("graph", "apply", "--change", "demo")

    result = _run("graph", "show", "--node", "cap:DB-001")

    assert result.exit_code == 0, result.output
    assert "[Capability / engineering]" in result.output
    assert "cap:DB-001 --implemented_by--> cmp:entities" in result.output


def test_show_node_for_a_missing_node_says_so_without_failing(repo):
    result = _run("graph", "show", "--node", "cmp:ghost")

    assert result.exit_code == 0, result.output
    assert "No node 'cmp:ghost'" in result.output


def test_show_filters_by_layer(repo):
    _changeset(repo, "demo", _minimal_ops())
    _run("graph", "apply", "--change", "demo")

    result = _run("graph", "show", "--layer", "technical")

    assert result.exit_code == 0, result.output
    assert "none" in result.output


# -- graph validate ------------------------------------------------------

def test_validate_on_a_clean_graph_exits_zero(repo):
    _changeset(repo, "demo", _minimal_ops())
    _run("graph", "apply", "--change", "demo")

    result = _run("graph", "validate")

    assert result.exit_code == 0, result.output
    assert "No errors" in result.output


def test_validate_exits_one_on_an_integrity_error(repo):
    _changeset(repo, "demo", [_component("alpha"), _edge("cmp:alpha", "depends_on", "cmp:ghost")])
    # The apply is refused, so write a bad graph directly to test validate.
    graph_path = repo / ".acsdd" / "graph" / "graph.json"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_text(json.dumps({"graph": {
        "meta": {"version": "1.0.0", "generated_at": "2026-08-10"},
        "nodes": [{"id": "cmp:a", "type": "Component", "name": "A"}],
        "edges": [{"id": "cmp:a|depends_on|cmp:ghost", "type": "depends_on",
                   "from": "cmp:a", "to": "cmp:ghost"}]}}), encoding="utf-8")

    result = _run("graph", "validate")

    assert result.exit_code == 1, result.output
    assert "dangling-edge" in result.output


def test_validate_exits_zero_on_a_warning_but_one_under_strict(repo):
    """Severity is which list a finding lands in, and --strict is the only
    thing that promotes a warning to a failure."""
    graph_path = repo / ".acsdd" / "graph" / "graph.json"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_text(json.dumps({"graph": {
        "meta": {"version": "1.0.0", "generated_at": "2026-08-10"},
        "nodes": [{"id": "ent:invoice", "type": "DataEntity", "name": "Invoice"}],
        "edges": []}}), encoding="utf-8")

    assert _run("graph", "validate").exit_code == 0
    strict = _run("graph", "validate", "--strict")
    assert strict.exit_code == 1, strict.output
    assert "orphan-node" in strict.output


def test_validate_schema_checks_the_document_on_disk(repo):
    """acsdd only ever writes documents the schema accepts, but it is not the
    only thing that can edit graph.json — a hand-edit or a bad merge lands
    here. This is also the only code path that loads the graph schema, and a
    schema nothing reads is one that silently stops matching reality."""
    graph_path = repo / ".acsdd" / "graph" / "graph.json"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_text(json.dumps({"graph": {
        "meta": {"version": "not-a-semver", "generated_at": "2026-08-10"},
        "nodes": [], "edges": []}}), encoding="utf-8")

    result = _run("graph", "validate")

    assert result.exit_code == 1, result.output
    assert "schema" in result.output
    assert "not-a-semver" in result.output


def test_validate_json_carries_the_counts_and_findings(repo):
    result = _run("graph", "validate", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["error_count"] == 0
    assert set(payload) >= {"errors", "warnings", "advisories", "node_count"}


def test_validate_with_a_change_replays_its_changeset(repo):
    """What gets validated is the graph as that change would leave it, so a
    problem is visible before anything is written."""
    _run("change", "new", "Guest checkout", "--id", "checkout-guest")
    _changeset(repo, "checkout-guest", [
        {"op": "add_node", "node": {"id": "req:checkout-guest.pay",
                                    "type": "Requirement", "name": "Pay as guest"}}])

    result = _run("graph", "validate", "--change", "checkout-guest")

    assert result.exit_code == 1, result.output
    assert "requirement-without-capability" in result.output


# -- graph apply ---------------------------------------------------------

def test_apply_writes_the_graph_and_cuts_a_revision(repo):
    path = _changeset(repo, "demo", _minimal_ops())

    result = _run("graph", "apply", str(path))

    assert result.exit_code == 0, result.output
    assert (repo / ".acsdd" / "graph" / "graph.json").is_file()
    assert "at revision 0001-" in result.output


def test_dry_run_writes_nothing(repo):
    path = _changeset(repo, "demo", _minimal_ops())

    result = _run("graph", "apply", str(path), "--dry-run")

    assert result.exit_code == 0, result.output
    assert "Would apply" in result.output
    assert not (repo / ".acsdd" / "graph" / "graph.json").exists(), \
        "--dry-run must not write the graph"


def test_applying_twice_changes_nothing_and_cuts_no_second_revision(repo):
    path = _changeset(repo, "demo", _minimal_ops())
    _run("graph", "apply", str(path))
    first = (repo / ".acsdd" / "graph" / "graph.json").read_text()

    result = _run("graph", "apply", str(path))

    assert result.exit_code == 0, result.output
    assert "Graph unchanged" in result.output
    assert (repo / ".acsdd" / "graph" / "graph.json").read_text() == first
    revisions = json.loads(
        (repo / ".acsdd" / "graph" / "revisions" / "index.json").read_text())
    assert len(revisions["revisions"]) == 1


def test_apply_refuses_an_integrity_error_and_writes_nothing(repo):
    path = _changeset(repo, "demo", [_component("alpha"),
                                  _edge("cmp:alpha", "depends_on", "cmp:ghost")])

    result = _run("graph", "apply", str(path))

    assert result.exit_code == 1, result.output
    assert "REFUSED" in result.output
    assert "dangling-edge" in result.output
    assert not (repo / ".acsdd" / "graph" / "graph.json").exists()


def test_force_does_not_override_an_integrity_error(repo):
    """Consent to rewrite your own graph is not consent to leave it
    inconsistent — the same line `capability remove` draws."""
    path = _changeset(repo, "demo", [_component("alpha"),
                                  _edge("cmp:alpha", "depends_on", "cmp:ghost")])

    result = _run("graph", "apply", str(path), "--force")

    assert result.exit_code == 1, result.output
    assert not (repo / ".acsdd" / "graph" / "graph.json").exists()


def test_apply_refuses_a_stale_base_revision(repo):
    _run("graph", "apply", str(_changeset(repo, "first", _minimal_ops())))
    stale = _changeset(repo, "second", [_component("billing"),
                                        _edge("cap:DB-001", "implemented_by", "cmp:billing")],
                       base_revision="0009-deadbeef")
    before = (repo / ".acsdd" / "graph" / "graph.json").read_text()

    result = _run("graph", "apply", str(stale))

    assert result.exit_code == 1, result.output
    assert "0009-deadbeef" in result.output
    assert (repo / ".acsdd" / "graph" / "graph.json").read_text() == before


def test_force_overrides_only_the_stale_base(repo):
    """The one thing --force means here."""
    _run("graph", "apply", str(_changeset(repo, "first", _minimal_ops())))
    stale = _changeset(repo, "second", [_component("billing"),
                                        _edge("cap:DB-001", "implemented_by", "cmp:billing")],
                       base_revision="0009-deadbeef")

    result = _run("graph", "apply", str(stale), "--force")

    assert result.exit_code == 0, result.output
    assert "at revision 0002-" in result.output


def test_apply_rejects_a_changeset_that_fails_the_schema(repo):
    path = repo / "bad.json"
    path.write_text(json.dumps({"changeset": {
        "id": "demo", "title": "A change",
        "operations": [{"op": "add_node", "node": {
            "id": "cmp:x", "type": "NotAType", "name": "X"}}]}}), encoding="utf-8")

    result = _run("graph", "apply", str(path))

    assert result.exit_code == 1, result.output
    assert "SCHEMA ERRORS" in result.output


def test_apply_reports_unparseable_json_without_a_traceback(repo):
    path = repo / "bad.json"
    path.write_text("{not json", encoding="utf-8")

    result = _run("graph", "apply", str(path))

    assert result.exit_code == 1, result.output
    assert "not parseable JSON" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_apply_json_reports_the_committed_revision(repo):
    path = _changeset(repo, "demo", _minimal_ops())

    result = _run("graph", "apply", str(path), "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["committed"].startswith("0001-")
    assert payload["is_noop"] is False
    assert payload["integrity"]["error_count"] == 0


def test_apply_marks_the_change_applied(repo):
    _run("change", "new", "A change", "--id", "demo")
    _changeset(repo, "demo", _minimal_ops())

    _run("graph", "apply", "--change", "demo")

    assert (repo / ".acsdd" / "changes" / "demo" / "applied.json").is_file()
    assert "applied as 0001-" in _run("change", "list").output


def test_apply_without_a_change_or_path_and_no_open_change_exits_one(repo):
    result = _run("graph", "apply")

    assert result.exit_code == 1, result.output
    assert "no open changes" in result.output


def test_apply_refuses_to_guess_between_two_open_changes(repo):
    """A wrong guess here silently writes into somebody else's change."""
    _run("change", "new", "One", "--id", "one")
    _run("change", "new", "Two", "--id", "two")

    result = _run("graph", "apply")

    assert result.exit_code == 1, result.output
    assert "more than one change is open" in result.output
    assert "- one" in result.output and "- two" in result.output


def test_apply_uses_the_single_open_change_without_being_told(repo):
    _run("change", "new", "Only one", "--id", "only")
    _changeset(repo, "only", _minimal_ops())

    result = _run("graph", "apply", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "No --change given, using: only" in result.output


# -- graph context -------------------------------------------------------

def test_context_json_advertises_the_vocabulary_and_rules(repo):
    from acsdd.graph.integrity import RULES
    from acsdd.graph.vocabulary import EDGE_TYPES, NODE_TYPES

    result = _run("graph", "context", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["vocabulary"]["node_types"]) == len(NODE_TYPES)
    assert len(payload["vocabulary"]["edge_types"]) == len(EDGE_TYPES)
    assert len(payload["rules"]) == len(RULES)
    assert [c["id"] for c in payload["capabilities"]] == \
        ["DB-001", "DB-002", "DB-003", "DB-004"]


def test_context_json_keeps_stdout_to_the_payload_alone(repo):
    """Chatter on stdout would break every consumer that pipes this to jq, so
    the auto-detect note has to go to stderr."""
    _run("change", "new", "Only one", "--id", "only-change")

    result = _run("graph", "context", "--json")

    assert result.exit_code == 0, result.output
    json.loads(result.stdout)  # raises if anything but the payload reached stdout
    assert "No --change given" in result.stderr


def test_context_human_mode_summarizes(repo):
    """A --json-only command would be the first in this tool and would be
    undebuggable by hand."""
    result = _run("graph", "context")

    assert result.exit_code == 0, result.output
    assert "13 node types, 10 edge types" in result.output
    assert "capabilities: 4" in result.output


def test_context_scopes_ids_to_the_change(repo):
    _run("change", "new", "Guest checkout", "--id", "checkout-guest")

    result = _run("graph", "context", "--json", "--change", "checkout-guest")

    payload = json.loads(result.output)
    assert payload["change"]["id_prefix"] == "checkout-guest."
    assert "checkout-guest." in payload["changeset_format"]["business_id_rule"]


# -- graph diff ----------------------------------------------------------

def test_diff_shows_what_a_changeset_would_do_without_applying(repo):
    path = _changeset(repo, "demo", _minimal_ops())

    result = _run("graph", "diff", str(path))

    assert result.exit_code == 0, result.output
    assert "cmp:entities" in result.output
    assert not (repo / ".acsdd" / "graph" / "graph.json").exists()


def test_diff_for_c4_classifies_components(repo):
    _run("graph", "apply", str(_changeset(repo, "first", _minimal_ops())))
    follow_up = _changeset(repo, "second", [
        _component("billing"),
        _edge("cmp:billing", "depends_on", "cmp:entities")])

    result = _run("graph", "diff", str(follow_up), "--for", "c4")

    assert result.exit_code == 0, result.output
    assert "NEW" in result.output and "cmp:billing" in result.output
    # cmp:entities gained an incoming edge, so it changed even though none of
    # its own fields did.
    assert "MODIFIED" in result.output and "cmp:entities" in result.output


def test_diff_for_c4_json_carries_the_counts(repo):
    _run("graph", "apply", str(_changeset(repo, "first", _minimal_ops())))
    follow_up = _changeset(repo, "second", [_component("billing")])

    result = _run("graph", "diff", str(follow_up), "--for", "c4", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["counts"]["new"] == 1
    assert payload["statuses"] == ["new", "modified", "removed", "related"]


def test_diff_exits_zero_even_when_the_changeset_would_be_refused(repo):
    """Informational. `graph apply --dry-run` is the command that judges."""
    path = _changeset(repo, "demo", [_component("alpha"),
                                     _edge("cmp:alpha", "depends_on", "cmp:ghost")])

    result = _run("graph", "diff", str(path))

    assert result.exit_code == 0, result.output


# -- graph revisions -----------------------------------------------------

def test_revisions_on_an_empty_repo_exits_zero(repo):
    result = _run("graph", "revisions")

    assert result.exit_code == 0, result.output
    assert "No revisions yet" in result.output


def test_revisions_lists_what_was_applied(repo):
    _run("graph", "apply", str(_changeset(repo, "demo", _minimal_ops())))

    result = _run("graph", "revisions")

    assert result.exit_code == 0, result.output
    assert "0001-" in result.output and "(demo)" in result.output


# -- change new / list / show / remove -----------------------------------

def test_change_new_creates_a_record_and_slugs_the_title(repo):
    result = _run("change", "new", "Guest Checkout Flow")

    assert result.exit_code == 0, result.output
    assert (repo / ".acsdd" / "changes" / "guest-checkout-flow" / "change.json").is_file()


def test_change_new_rejects_an_unusable_id(repo):
    result = _run("change", "new", "A change", "--id", "Nope Bad")

    assert result.exit_code == 1, result.output
    assert "not a usable change id" in result.output


def test_change_new_refuses_to_clobber_without_force(repo):
    _run("change", "new", "First", "--id", "demo")
    record = repo / ".acsdd" / "changes" / "demo" / "change.json"
    before = record.read_text()

    result = _run("change", "new", "Second", "--id", "demo")

    assert result.exit_code == 1, result.output
    assert "already exists" in result.output
    assert record.read_text() == before, "the record must be untouched"


def test_change_new_with_force_overwrites(repo):
    _run("change", "new", "First", "--id", "demo")

    result = _run("change", "new", "Second", "--id", "demo", "--force")

    assert result.exit_code == 0, result.output
    assert "Second" in _run("change", "show", "demo").output


def test_change_list_on_an_empty_repo(repo):
    result = _run("change", "list")

    assert result.exit_code == 0, result.output
    assert "No changes yet" in result.output


def test_change_show_reports_the_changeset_state(repo):
    _run("change", "new", "A change", "--id", "demo")

    before = _run("change", "show", "demo")
    assert "changeset: none written yet" in before.output

    _changeset(repo, "demo", _minimal_ops())
    after = _run("change", "show", "demo", "--json")
    payload = json.loads(after.output)
    assert payload["has_changeset"] is True
    assert payload["operation_count"] == 3


def test_change_show_on_a_missing_change_exits_one(repo):
    result = _run("change", "show", "nope")

    assert result.exit_code == 1, result.output
    assert "no change 'nope'" in result.output


def test_change_remove_refuses_without_force_and_deletes_nothing(repo):
    """The assertion that matters is the files, not the exit code: a command
    that prints the refusal *and* deletes would pass the weaker one."""
    _run("change", "new", "A change", "--id", "demo")
    _changeset(repo, "demo", _minimal_ops())
    record = repo / ".acsdd" / "changes" / "demo" / "change.json"
    changeset = repo / ".acsdd" / "changes" / "demo" / "changeset.json"

    result = _run("change", "remove", "demo")

    assert result.exit_code == 1, result.output
    assert "Would remove:" in result.output
    assert record.exists() and changeset.exists(), "nothing may be deleted without --force"


def test_change_remove_with_force_deletes_and_prunes_the_change_dir(repo):
    _run("change", "new", "A change", "--id", "demo")
    _changeset(repo, "demo", _minimal_ops())

    result = _run("change", "remove", "demo", "--force")

    assert result.exit_code == 0, result.output
    assert not (repo / ".acsdd" / "changes" / "demo").exists()
    # The changes root survives its last change: a directory that vanished
    # would read as "this repo never had a graph".
    assert (repo / ".acsdd" / "changes").is_dir()


def test_change_remove_on_a_missing_change_exits_one(repo):
    result = _run("change", "remove", "nope", "--force")

    assert result.exit_code == 1, result.output
    assert "No artifacts for change 'nope'" in result.output


def test_change_remove_warns_that_it_does_not_revert_an_applied_change(repo):
    _run("change", "new", "A change", "--id", "demo")
    _changeset(repo, "demo", _minimal_ops())
    _run("graph", "apply", "--change", "demo")

    result = _run("change", "remove", "demo", "--force")

    assert result.exit_code == 0, result.output
    assert "does not revert the graph" in result.output
    assert (repo / ".acsdd" / "graph" / "graph.json").is_file()


def test_change_remove_never_auto_detects(repo):
    """A bad guess costs files here, not an error message — the same reason
    `profile remove` takes a required argument."""
    result = _run("change", "remove")

    assert result.exit_code == 2, result.output
    assert "Missing argument" in result.output


# -- the whole round trip ------------------------------------------------

def test_the_full_prd_import_flow(repo):
    """change new -> context -> changeset -> dry-run -> apply -> validate,
    which is the flow the graph-import skill drives."""
    assert _run("change", "new", "Guest checkout", "--id", "checkout-guest").exit_code == 0

    context = json.loads(_run("graph", "context", "--json").stdout)
    assert context["change"]["id"] == "checkout-guest"

    _changeset(repo, "checkout-guest", [
        _capability("DB-001"),
        _component("entities"),
        _edge("cap:DB-001", "implemented_by", "cmp:entities"),
        {"op": "add_node", "node": {
            "id": "req:checkout-guest.pay", "type": "Requirement",
            "name": "Pay without an account"}},
        _edge("req:checkout-guest.pay", "delivered_by", "cap:DB-001"),
    ], base_revision=context["graph_revision"])

    dry = _run("graph", "apply", "--change", "checkout-guest", "--dry-run", "--json")
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["integrity"]["error_count"] == 0

    assert _run("graph", "apply", "--change", "checkout-guest").exit_code == 0
    strict = _run("graph", "validate", "--change", "checkout-guest", "--strict")
    assert strict.exit_code == 0, strict.output


def test_a_second_change_is_not_blamed_for_the_first_changes_ids(repo):
    """The bug the owned-node split exists to prevent: without it, every apply
    after the first one fails on the previous change's perfectly good ids."""
    _changeset(repo, "first", [
        _capability("DB-001"), _component("entities"),
        _edge("cap:DB-001", "implemented_by", "cmp:entities"),
        {"op": "add_node", "node": {"id": "req:first.pay", "type": "Requirement",
                                    "name": "Pay"}},
        _edge("req:first.pay", "delivered_by", "cap:DB-001")])
    assert _run("graph", "apply", "--change", "first").exit_code == 0

    _changeset(repo, "second", [
        {"op": "add_node", "node": {"id": "req:second.refund", "type": "Requirement",
                                    "name": "Refund"}},
        _edge("req:second.refund", "delivered_by", "cap:DB-001")])

    result = _run("graph", "apply", "--change", "second")

    assert result.exit_code == 0, result.output
    assert "business-id-scoped-to-change" not in result.output
