"""Layout resolution: the new .acsdd/ root plus the pre-.acsdd fallback."""

from acsdd.paths import (
    change_artifact_paths,
    profile_artifact_paths,
    resolve_acsdd_root,
    resolve_capabilities_dir,
    resolve_changes_dir,
    resolve_graph_dir,
    resolve_profiles_dir,
)


# ---------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------

def test_profiles_resolves_new_layout(tmp_path):
    (tmp_path / ".acsdd" / "profiles").mkdir(parents=True)

    resolved, is_legacy = resolve_profiles_dir(tmp_path)

    assert resolved == tmp_path / ".acsdd" / "profiles"
    assert is_legacy is False


def test_profiles_falls_back_to_legacy_layout(tmp_path):
    (tmp_path / "acsdd" / "profiles").mkdir(parents=True)

    resolved, is_legacy = resolve_profiles_dir(tmp_path)

    assert resolved == tmp_path / "acsdd" / "profiles"
    assert is_legacy is True


def test_profiles_prefers_new_layout_when_both_exist(tmp_path):
    (tmp_path / ".acsdd" / "profiles").mkdir(parents=True)
    (tmp_path / "acsdd" / "profiles").mkdir(parents=True)

    resolved, is_legacy = resolve_profiles_dir(tmp_path)

    assert resolved == tmp_path / ".acsdd" / "profiles"
    assert is_legacy is False


def test_profiles_defaults_to_new_layout_when_neither_exists(tmp_path):
    # The fallback doubles as the write target, so it must be the new path.
    resolved, is_legacy = resolve_profiles_dir(tmp_path)

    assert resolved == tmp_path / ".acsdd" / "profiles"
    assert is_legacy is False


def test_profiles_does_not_walk_up(tmp_path):
    # Deliberately cwd-relative: it has to match `profile discover --output`,
    # which is resolved against wherever the user ran it from.
    (tmp_path / ".acsdd" / "profiles").mkdir(parents=True)
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)

    resolved, _ = resolve_profiles_dir(nested)

    assert resolved == nested / ".acsdd" / "profiles"


# ---------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------

def test_capabilities_walks_up_to_new_layout(tmp_path):
    (tmp_path / ".acsdd" / "capabilities" / "_manifests").mkdir(parents=True)
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)

    resolved, is_legacy = resolve_capabilities_dir(nested)

    assert resolved == tmp_path / ".acsdd" / "capabilities"
    assert is_legacy is False


def test_capabilities_walks_up_to_legacy_layout(tmp_path):
    (tmp_path / "capabilities" / "_manifests").mkdir(parents=True)
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)

    resolved, is_legacy = resolve_capabilities_dir(nested)

    assert resolved == tmp_path / "capabilities"
    assert is_legacy is True


def test_capabilities_prefers_new_layout_at_the_same_level(tmp_path):
    (tmp_path / ".acsdd" / "capabilities" / "_manifests").mkdir(parents=True)
    (tmp_path / "capabilities" / "_manifests").mkdir(parents=True)

    resolved, is_legacy = resolve_capabilities_dir(tmp_path)

    assert resolved == tmp_path / ".acsdd" / "capabilities"
    assert is_legacy is False


def test_capabilities_nearest_repo_wins_over_distant_new_layout(tmp_path):
    # Both candidates are checked at each level before ascending. A per-layout
    # walk (all levels for .acsdd, then all levels for legacy) would pick the
    # outer repo here, which is the wrong repository entirely.
    (tmp_path / ".acsdd" / "capabilities" / "_manifests").mkdir(parents=True)
    inner = tmp_path / "vendor" / "other-repo"
    (inner / "capabilities" / "_manifests").mkdir(parents=True)

    resolved, is_legacy = resolve_capabilities_dir(inner)

    assert resolved == inner / "capabilities"
    assert is_legacy is True


def test_capabilities_defaults_to_new_layout_when_none_found(tmp_path):
    resolved, is_legacy = resolve_capabilities_dir(tmp_path)

    assert resolved == tmp_path / ".acsdd" / "capabilities"
    assert is_legacy is False


def test_capabilities_ignores_a_tree_without_manifests(tmp_path):
    # An empty capabilities/ dir isn't an adopted layout — a repo can have a
    # directory by that name for its own reasons.
    (tmp_path / "capabilities").mkdir()

    resolved, is_legacy = resolve_capabilities_dir(tmp_path)

    assert resolved == tmp_path / ".acsdd" / "capabilities"
    assert is_legacy is False


# ---------------------------------------------------------------------
# profile artifact set
# ---------------------------------------------------------------------

def test_profile_artifact_paths_covers_all_four_files(tmp_path):
    paths = profile_artifact_paths(tmp_path, "demo")

    assert [p.name for p in paths] == [
        "demo-draft.yaml",
        "demo-discovery-report.md",
        "demo-recommendations.md",
        "demo.yaml",
    ]
    assert all(p.parent == tmp_path for p in paths)


def test_profile_artifact_paths_can_exclude_the_finalized_profile(tmp_path):
    # What `profile discover` writes, and therefore what its overwrite guard
    # is allowed to trip on — a finalized profile next door must not block it.
    paths = profile_artifact_paths(tmp_path, "demo", include_finalized=False)

    assert tmp_path / "demo.yaml" not in paths
    assert len(paths) == 3


def test_profile_artifact_paths_does_not_require_the_files_to_exist(tmp_path):
    assert not any(p.exists() for p in profile_artifact_paths(tmp_path, "nope"))


# ---------------------------------------------------------------------
# graph + changes
# ---------------------------------------------------------------------

def test_graph_dir_resolves_under_the_acsdd_root(tmp_path):
    (tmp_path / ".acsdd" / "graph").mkdir(parents=True)

    assert resolve_acsdd_root(tmp_path) == tmp_path / ".acsdd"
    assert resolve_graph_dir(tmp_path) == tmp_path / ".acsdd" / "graph"


def test_graph_dir_walks_up_from_a_subdirectory(tmp_path):
    # The graph is repo-scoped, so running a graph command from src/ has to
    # find the same graph as running it from the root.
    (tmp_path / ".acsdd" / "graph").mkdir(parents=True)
    deep = tmp_path / "src" / "Domain" / "Payment"
    deep.mkdir(parents=True)

    assert resolve_graph_dir(deep) == tmp_path / ".acsdd" / "graph"


def test_an_acsdd_root_is_recognized_by_profiles_or_capabilities_too(tmp_path):
    # A repo that has only been through `profile discover` must anchor on its
    # own .acsdd rather than walking past it into a parent's.
    (tmp_path / ".acsdd" / "profiles").mkdir(parents=True)

    assert resolve_acsdd_root(tmp_path) == tmp_path / ".acsdd"


def test_graph_and_changes_resolve_off_the_same_root(tmp_path):
    # Two independent walk-ups would let a distant ancestor's changes/ pair
    # with the local graph/ — the bug resolve_capabilities_dir documents.
    parent = tmp_path / "outer"
    (parent / ".acsdd" / "changes").mkdir(parents=True)
    repo = parent / "inner"
    (repo / ".acsdd" / "graph").mkdir(parents=True)

    assert resolve_graph_dir(repo) == repo / ".acsdd" / "graph"
    assert resolve_changes_dir(repo) == repo / ".acsdd" / "changes"


def test_graph_dir_falls_back_to_cwd_when_nothing_exists(tmp_path):
    # Where a first graph should be created.
    assert resolve_graph_dir(tmp_path) == tmp_path / ".acsdd" / "graph"
    assert resolve_changes_dir(tmp_path) == tmp_path / ".acsdd" / "changes"


def test_change_artifact_paths_covers_all_three_files(tmp_path):
    paths = change_artifact_paths(tmp_path, "checkout-guest")

    assert [p.name for p in paths] == ["change.json", "changeset.json", "applied.json"]
    assert all(p.parent == tmp_path / "checkout-guest" for p in paths)


def test_change_artifact_paths_does_not_require_the_files_to_exist(tmp_path):
    assert not any(p.exists() for p in change_artifact_paths(tmp_path, "nope"))
