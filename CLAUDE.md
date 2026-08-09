# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`acsdd` is a CLI for the ACSDD (AI-Collaborative Software Development &
Delivery) framework: capability manifests, a generated capability catalog,
and repository engineering profiles. The repo ships both the tool
(`src/acsdd/`) and a working example of the data it operates on
(`.acsdd/capabilities/` — four real capabilities, `DB-001`..`DB-004`, for a
Symfony 4.4 / Doctrine / MySQL 8 stack) so every command can be run
immediately against something real.

## Commands

```bash
# Install from source (dev)
pip install -e ".[dev]"

# Run the full test suite
pytest

# Run a single test file / test
pytest tests/test_capability.py
pytest tests/test_capability.py::test_validate_catalog_circular_dependency

# Exercise the CLI against the repo's own example data
acsdd capability validate
acsdd catalog verify
acsdd profile discover /path/to/repo   # --profile-id defaults to the dir name (here: "repo")

# Onboarding a brand-new repo: discover its profile, review + finalize it,
# then scaffold a draft capability manifest from it (auto-creates
# .acsdd/capabilities/_manifests/ if missing) — the workflow this tool is
# designed around.
acsdd profile discover /path/to/my-project
acsdd profile review    # what's still [REVIEW REQUIRED], and how to resolve it
# ...fill in those fields in the draft, then:
# (both --draft below and --profile further down default to whatever's
# under ./.acsdd/profiles — spelled out here only for clarity)
acsdd profile create --draft .acsdd/profiles/my-project-draft.yaml
acsdd capability generate --id BE-001 --category BE

# Install the packaged Claude Code skill that automates the review step
acsdd skill install

# Undo any of the above. Every `remove` lists what it would delete and does
# nothing without --force (see "Removal" under Architecture).
acsdd capability remove BE-001 --force
acsdd profile remove my-project --force
acsdd skill remove profile-review --force

# Build the standalone PyInstaller binary locally
pip install . pyinstaller
pyinstaller packaging/acsdd.spec --distpath dist --workpath build --clean
./dist/acsdd --version

# Self-update a standalone binary install in place (no-op / errors on a
# source install — see src/acsdd/update.py)
acsdd update
```

There is no lint/format tooling configured in `pyproject.toml` — don't
invent a lint step.

## Architecture

**Entry point:** `src/acsdd/cli.py` defines one Click group (`acsdd`) with
four subcommand groups — `capability`, `catalog`, `profile`, `skill` — plus
one top-level command, `update`. Three of the groups carry a `remove`
subcommand (see **Removal** below). All are thin wrappers: they resolve a
manifests directory, call into the matching non-CLI module below, and format
output. Keep business logic out of `cli.py`; put it in the module it belongs
to so it stays testable without invoking Click.

**`paths.py`** — the single authority on where acsdd's artifacts live inside
a consumer repo. Everything it writes goes under one hidden `.acsdd/` root
(`.acsdd/profiles/`, `.acsdd/capabilities/`); the sole exception is
`.claude/skills/`, which is Claude Code's convention and stays put (see
`skills.py`). `resolve_profiles_dir` / `resolve_capabilities_dir` each return
`(dir, is_legacy)`, falling back to the pre-`.acsdd` locations (`acsdd/profiles`,
top-level `capabilities/`) so a repo onboarded before the move keeps working —
but nothing ever *writes* to a legacy path again. **Don't spell either layout
out anywhere else**; call a resolver. `resolve_capabilities_dir` checks both
candidates at *each* level of its walk-up before ascending — two sequential
per-layout walks would let a distant ancestor's `.acsdd/capabilities` beat the
legacy `capabilities/` of the repo you're standing in.

`profile_artifact_paths` lives here for the same reason: "which four files make
up one profile" (draft, finalized, discovery report, recommendations) is a
layout question. `profile remove` wants all four; `profile discover`'s overwrite
guard passes `include_finalized=False` because it must stay blind to a finalized
profile sitting next to the draft it's about to rewrite.

`cli.py`'s `_default_capabilities_dir` / `_profiles_dir` wrap those resolvers
and emit the legacy nudge via `_warn_legacy_layout` — stderr only (stdout
carries `profile review --json`), once per kind per invocation, reset in the
`cli` group callback rather than per-process so the test suite sees each
invocation fresh.

Profile auto-detection (`_default_profile_path` in `cli.py`, used by
`capability generate` and `profile validate` when their path argument is
omitted) is cwd-relative with no walk-up, matching `profile discover`'s own
`--output` default (`paths.DEFAULT_PROFILES_DIR`) exactly. It prefers a
finalized profile (`<id>.yaml`) over a draft (`<id>-draft.yaml`), and
deliberately returns `None` (forcing an explicit path) rather than guessing
whenever more than one plausible candidate exists. `_default_draft_profile_path`
is the same idea for `profile create --draft`, but only ever matches
`*-draft.yaml` — finalizing an already-finalized profile isn't the point of
that command, so it must not silently pick one up.
`_default_review_profile_path` composes the two for `profile review`, trying
the draft *first*: `_default_profile_path`'s preference is inverted for that
command, since a finalized profile by definition has nothing left to review.

**`capability/`** — manifest loading, validation, generation, and removal.
- `loader.py`: `load_manifest`/`iter_manifests` — YAML I/O only, no schema
  checks. Raises `ManifestLoadError` on unparseable YAML.
- `validator.py`: `validate_manifest` (per-file JSON Schema check against
  `schemas/capability.schema.json`, Draft 2020-12) and `validate_catalog`
  (cross-manifest checks a single-file schema can't express: dependency
  refs resolve, no self-deps, no circular dependency chains via DFS).
- `generator.py`: `scaffold_manifest` — pure function backing `acsdd
  capability generate`. Takes a loaded profile dict (as produced by
  `profile discover`) and returns a schema-shaped manifest dict: fields
  derivable from the profile (adapter `stack` from
  `capability_configuration.default_adapter`, `profile_constraints` from
  `technology_stack`, `quality_gates` from `quality_gates.automatic`) are
  filled in; fields requiring actual knowledge of the specific capability
  (name, description, concrete inputs/outputs) are left as `[REVIEW
  REQUIRED]` placeholders. This is the only code in the repo that reads
  `capability_configuration.default_adapter` — everywhere else it's written
  by `profile discover` and never consumed, a naming convention rather than
  an enforced link.
- `remover.py`: `plan_removal`/`find_dependents` — works out what `acsdd
  capability remove` would delete and what it would break, without deleting
  anything (that's `removal.remove_paths`). It finds the procedure doc via
  `builder.find_doc_file` rather than reconstructing `generate`'s
  `<cap-id-lower>.md` filename, so a hand-renamed doc still gets cleaned up.
  `find_dependents` walks the same edge set as `validate_catalog`, which is
  what makes "refuse if non-empty" equivalent to "never create a dangling ref."

**`catalog/builder.py`** — `build_catalog_markdown` regenerates
`.acsdd/capabilities/CATALOG.md` purely from the manifest dicts (grouped by a
fixed `CATEGORY_ORDER`, with a rendered dependency graph and best-effort
procedure-doc links via `_doc_link`, a formatter over `find_doc_file`, which
greps category doc folders for a matching capability id — matching on *content*
rather than filename, since docs predate the scaffolder's naming convention).
**`CATALOG.md` is a generated file** — `acsdd catalog verify` diffs a fresh
render against the checked-in file (ignoring the `**Last generated:**` date) and
fails if manifests and catalog have drifted, or if any manifest is invalid.
`capability remove` rebuilds it inline for the same reason.

**`profile/`** — repository discovery, review, validation, and finalization.
- `_discovery_impl.py` (the bulk of the logic, ~1000 lines) implements
  PROFILE-001 style discovery as a pipeline of detector classes:
  `StackDetector` → `SymfonyStructureDetector` (backend-specific
  enrichment) → `ConventionDetector` → `ArchitectureDetector` →
  `HealthMetrics` → `ProfileGenerator`, then
  `generate_discovery_report`/`generate_recommendations` render the
  findings to markdown. `cli.py`'s `profile discover` command calls
  `_discovery_impl.main()` in-process (not a subprocess) — there is no
  separate script boundary here. Every profile it emits has
  `meta.status: "draft"` hardcoded — nothing in `_discovery_impl.py` ever
  changes that.
- `validator.py`: `validate_profile_file` — same JSON Schema pattern as
  `capability/validator.py`, against `schemas/profile.schema.json`.
  Structural shape only — it has no concept of "is this profile actually
  finished," which is why `generator.py` exists.
- `generator.py`: backs `acsdd profile create`. `find_unresolved_fields`
  recursively scans a profile dict for any string starting with
  `"[REVIEW REQUIRED"` (prefix match, since `_discovery_impl.py` emits
  varied suffixes) and returns their dotted/indexed paths; `finalize_profile`
  (only called once that list is empty) flips `meta.status` to `"active"`
  and bumps `meta.version` from the draft default `0.1.0` to `1.0.0` — this
  is the only place in the codebase that ever changes a profile's status
  away from `"draft"`.
  **The prefix match is load-bearing.** Every placeholder `_discovery_impl.py`
  emits must *start* with `[REVIEW REQUIRED` — a marker appended to a detected
  value is invisible to `find_unresolved_fields`, and therefore to
  `validate --strict`, `profile create`'s gate, and `profile review` all at
  once. That was a real bug at `_discovery_impl.py`'s low-confidence frontend
  branch; keep the detected value *inside* the placeholder instead.
- `profile remove` has no module of its own: the file set comes from
  `paths.profile_artifact_paths` and the deletion from `removal.remove_paths`,
  leaving nothing for a `profile/remover.py` to hold. Its profile id is a
  required argument — the auto-detection the other profile commands do is
  wrong here, where a bad guess costs files rather than an error message.
- `review.py`: backs `acsdd profile review`. Reuses `find_unresolved_fields`
  (never re-implements the scan — that shared definition is what keeps the
  three consumers agreeing) and maps each path to a `FieldGuidance` record:
  what the field means, what detection was attempted and why it missed, which
  files to inspect, allowed/example values, and whether it's fixable by editing
  or only by re-running discovery. Lookup is index-normalized
  (`...constraints[3].tool` → `...constraints[].tool`), then refined by the
  sibling `id` for the cross-cutting constraints — the list index is an
  artifact of `ProfileGenerator`'s literal ordering, so keying on it would
  break the moment someone reorders the list. `guidance_for` never returns
  `None` and never raises: an unregistered path degrades to echoing the raw
  placeholder, which is why the drift test in `tests/test_discovery.py` (real
  discovery output, every placeholder must have `has_guidance: true`) matters —
  it's what stops that fallback becoming the normal case as new emission sites
  land. The module is a static hint table plus `Path.exists()` probes; it
  deliberately does not re-run detectors. Unlike its two siblings, the command
  exits **0** when it finds unresolved fields — it's informational, not a gate.

**`skills.py` + `assets/`** — backs `acsdd skill list/install/show/remove`,
which copies the Claude Code skills acsdd ships into a consumer repo's
`.claude/skills/`. `assets/claude/skills/<name>/SKILL.md` mirrors the
destination layout so installing is a straight copy. The split with `review.py`
is deliberate and worth preserving: **per-field knowledge lives in `review.py`,
procedure lives in `SKILL.md`.** The skill drives itself off
`acsdd profile review --json` rather than restating detection facts in prose,
which would go stale the first time a detector changed. `remove_skill` mirrors
`install_skill` down to reporting a no-op through its result rather than
raising; it cleans up the emptied `.claude/skills/<name>/` but never
`.claude/skills/` itself, which hosts other tools' skills too.

**`removal.py`** — `remove_paths(paths, protect=...)`, the one deletion
primitive, shared by all three `remove` commands. *What* to delete is answered
per-artifact elsewhere (`capability.remover`, `paths.profile_artifact_paths`,
`skills.remove_skill`); this only unlinks and then prunes the directories it
emptied. `protect` exists because two directories are structural rather than
incidental: `_manifests/` is how `resolve_capabilities_dir` recognizes a
capabilities tree at all, and a profiles directory that vanished with its last
profile would read as "never onboarded". Pruning stops at immediate parents so
emptying `backend/` can't cascade up into the capabilities root.

**Removal is `--force`-guarded, never prompted.** `cli.py`'s
`_require_force_to_remove` lists what would go and exits 1 without the flag.
That's the same shape as every other destructive edge in the tool (`already
exists — re-run with --force to overwrite`) and keeps the commands usable
unattended; there is deliberately no `click.confirm` anywhere in this codebase.
The one thing `--force` does *not* override is `capability remove`'s dependent
check — consent to delete your own files isn't consent to leave dangling refs
in everyone else's manifests.

**`update.py`** — backs `acsdd update`, self-updating a standalone binary
install in place. Guarded by `is_frozen_binary()` (PyInstaller's `sys.frozen`
marker) — refuses to run under a source/pip install, since there's no
single executable file to replace there. Reimplements `install.sh`'s
platform-detection/download/checksum-verify steps in Python (stdlib
`urllib`, no `curl` dependency at runtime) rather than shelling out to the
script; downloads to a temp dir *inside* the existing binary's directory
(`tempfile.TemporaryDirectory(dir=current_exe.parent)`) so the final
`os.replace()` onto the running executable is an atomic same-filesystem
rename, safe to do while the old binary is still executing. The asset
naming convention (`acsdd-{os}-{arch}`) is duplicated between `install.sh`
and this module — if `release.yml`'s matrix or target naming ever changes,
update both.

**Packaged data files** — two packages ship non-Python files: `schemas/` (the
two JSON Schemas, loaded via
`importlib.resources.files("acsdd.schemas")`) and `assets/` (the Claude Code
skills, loaded via `importlib.resources.files("acsdd.assets")`). Neither is
ever addressed by a relative filesystem path or one derived from `__file__`.

Any new file in either must be registered in **both**
`[tool.setuptools.package-data]` in `pyproject.toml` **and** the `datas` list
in `packaging/acsdd.spec` — PyInstaller doesn't pick up setuptools
package-data automatically, so missing the second one silently 404s at runtime
in the standalone binary while working fine from source. `release.yml`'s smoke
test installs the skill from each built binary specifically to catch that
desync before publish.

## Distribution: two install paths that must stay in sync

1. **`pip install -e ".[dev]"`** — for contributors, from source.
2. **`curl -fsSL .../install.sh | sh`** — installs a prebuilt PyInstaller
   binary from GitHub Releases, no Python required. Built via
   `packaging/acsdd.spec` / `packaging/acsdd_entry.py`, published by
   `.github/workflows/release.yml` on any `vX.Y.Z` tag push (linux x86_64 +
   arm64, macOS Apple Silicon/arm64 only — Intel macOS was dropped after
   GitHub's hosted `macos-13` runner sat queued indefinitely and blocked the
   whole release, since the publish job needs every matrix leg to finish;
   each binary is smoke-tested against `.acsdd/capabilities/` before upload,
   checksummed).

If you change how the CLI locates data files (schemas, assets, or anything
else loaded via `importlib.resources`), verify both paths: `pytest` covers the
source path, but only building the binary
(`pyinstaller packaging/acsdd.spec ...`) and running it with `src/` off
`PYTHONPATH` proves the frozen path still works — e.g.
`cd $(mktemp -d) && env -u PYTHONPATH /abs/path/dist/acsdd skill install`.

## Versioning

The version is duplicated in two places that must always match:
`project.version` in `pyproject.toml` and `__version__` in
`src/acsdd/__init__.py` (which `cli.py` surfaces via `--version` and
`acsdd update`'s "Current version:" line). Follow semver: patch for bug
fixes, minor for backwards-compatible features, major for breaking changes
to the CLI or manifest/profile schemas.

Bump the version in its own commit, *after* the fix/feature commit(s) it
covers have landed on `main` — not bundled into the same commit as the
change. Commit message: `Bump version to X.Y.Z`, with a body summarizing
what's in the release (see `f9c8b98`, `0894833`, `f42aa7e` for examples).
This keeps the change itself reviewable independent of the version number,
and keeps "what shipped in X.Y.Z" answerable straight from `git log`
without a separate changelog file (there isn't one).

The mechanical part of this — editing both files in lockstep, committing,
tagging — is automated by `.github/workflows/bump-version.yml`
(`workflow_dispatch`, run from the Actions tab or `gh workflow run
bump-version.yml -f version=X.Y.Z -f summary="..."`). It still requires a
human to supply the version number and release summary; it does not infer
either. It pushes straight to `main`, so only run it once the commit(s)
being released are already there.

A release only actually publishes binaries when the bump commit is tagged
`vX.Y.Z` and the tag is pushed — `.github/workflows/release.yml` triggers
on that tag pattern. An unbumped or untagged fix on `main` is invisible to
`acsdd update` and to `curl .../install.sh`, both of which only ever see
the latest GitHub release.

## Testing conventions

Tests live in `tests/`, generally one file per package (`test_capability.py`,
`test_profile.py`, `test_discovery.py`, `test_capability_generator.py`,
`test_profile_generator.py`, `test_profile_review.py`, `test_paths.py`,
`test_skills.py`, `test_update.py`), plain `pytest` functions (no
test classes). `test_update.py` monkeypatches `is_frozen_binary`/
`detect_platform`/`_download` rather than hitting the network or a real
binary — keep that pattern for any future changes there. A
`real_manifests_dir` fixture
(see `tests/test_capability.py`) points at the actual
`.acsdd/capabilities/_manifests/` example data — several tests validate against
that real data directly rather than only synthetic fixtures, so changes to
the example capabilities can break tests even if the tool code is untouched.

The `remove` commands have no test file of their own; each one's tests sit with
its create counterpart (`test_capability_generator.py`, `test_profile_generator.py`,
`test_skills.py`), because the useful test is the round trip. Every
no-`--force` test must assert the files are **still on disk** afterwards, not
just that the exit code was 1 — a command that prints the refusal *and* deletes
would pass the weaker assertion.

Two tests are guards rather than coverage, and shouldn't be weakened to make a
change pass:

- `test_discovery.py::test_review_covers_every_placeholder_real_discovery_emits`
  runs real discovery against the fixture repo and asserts every placeholder it
  emits has a registered entry in `review.py`. Adding an emission site to
  `_discovery_impl.py` without guidance fails here — that's the point.
- `test_profile_review.py::test_allowed_values_match_the_json_schema` reads
  `profile.schema.json` and checks the enums `review.py` keeps as plain
  constants still match it.

`test_skills.py` covers the packaged-asset load path for the *source* install
only; the frozen half can't be tested from pytest and is asserted by
`release.yml`'s smoke-test step instead.
