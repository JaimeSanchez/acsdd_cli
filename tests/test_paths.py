"""Layout resolution: the new .acsdd/ root plus the pre-.acsdd fallback."""

from acsdd.paths import resolve_capabilities_dir, resolve_profiles_dir


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
