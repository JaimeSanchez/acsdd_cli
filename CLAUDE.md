# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`acsdd` is a CLI for the ACSDD (AI-Collaborative Software Development &
Delivery) framework: capability manifests, a generated capability catalog,
and repository engineering profiles. The repo ships both the tool
(`src/acsdd/`) and a working example of the data it operates on
(`capabilities/` — four real capabilities, `DB-001`..`DB-004`, for a
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
# capabilities/_manifests/ if missing) — the workflow this tool is
# designed around.
acsdd profile discover /path/to/my-project
# ...fill in any [REVIEW REQUIRED] fields in the draft, then:
# (both --draft below and --profile further down default to whatever's
# under ./acsdd/profiles — spelled out here only for clarity)
acsdd profile create --draft acsdd/profiles/my-project-draft.yaml
acsdd capability generate --id BE-001 --category BE

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
three subcommand groups — `capability`, `catalog`, `profile` — plus one
top-level command, `update` — that are thin wrappers: they resolve a
manifests directory, call into the matching non-CLI module below, and format
output. Keep business logic out of `cli.py`; put it in the module it belongs
to so it stays testable without invoking Click.

Manifests directory resolution (`_default_capabilities_dir` in `cli.py`)
walks up from `cwd` looking for `capabilities/_manifests`, so the CLI works
from any subdirectory of a project that has adopted the ACSDD layout — don't
hardcode `./capabilities` elsewhere.

Profile auto-detection (`_default_profile_path` in `cli.py`, used by
`capability generate` and `profile validate` when their path argument is
omitted) only looks in `./acsdd/profiles` — cwd-relative, no walk-up —
matching `profile discover`'s own `--output` default exactly. It prefers a
finalized profile (`<id>.yaml`) over a draft (`<id>-draft.yaml`), and
deliberately returns `None` (forcing an explicit path) rather than guessing
whenever more than one plausible candidate exists. `_default_draft_profile_path`
is the same idea for `profile create --draft`, but only ever matches
`*-draft.yaml` — finalizing an already-finalized profile isn't the point of
that command, so it must not silently pick one up.

**`capability/`** — manifest loading, validation, and generation.
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

**`catalog/builder.py`** — `build_catalog_markdown` regenerates
`capabilities/CATALOG.md` purely from the manifest dicts (grouped by a
fixed `CATEGORY_ORDER`, with a rendered dependency graph and best-effort
procedure-doc links via `_doc_link`, which greps category doc folders for a
matching capability id). **`CATALOG.md` is a generated file** — `acsdd
catalog verify` diffs a fresh render against the checked-in file (ignoring
the `**Last generated:**` date) and fails if manifests and catalog have
drifted, or if any manifest is invalid.

**`profile/`** — repository discovery, profile validation, and finalization.
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

**`schemas/`** — the two JSON Schemas are loaded at runtime via
`importlib.resources.files("acsdd.schemas")`, not by relative filesystem
path. Any new schema file must be added to
`[tool.setuptools.package-data]` in `pyproject.toml` **and** to the
`datas` list in `packaging/acsdd.spec` (PyInstaller doesn't pick up
setuptools package-data automatically) or it will silently 404 at runtime
in the standalone binary while working fine from source.

## Distribution: two install paths that must stay in sync

1. **`pip install -e ".[dev]"`** — for contributors, from source.
2. **`curl -fsSL .../install.sh | sh`** — installs a prebuilt PyInstaller
   binary from GitHub Releases, no Python required. Built via
   `packaging/acsdd.spec` / `packaging/acsdd_entry.py`, published by
   `.github/workflows/release.yml` on any `vX.Y.Z` tag push (linux x86_64 +
   arm64, macOS Apple Silicon/arm64 only — Intel macOS was dropped after
   GitHub's hosted `macos-13` runner sat queued indefinitely and blocked the
   whole release, since the publish job needs every matrix leg to finish;
   each binary is smoke-tested against `capabilities/` before upload,
   checksummed).

If you change how the CLI locates data files (schemas, or anything else
loaded via `importlib.resources`), verify both paths: `pytest` covers the
source path, but only building the binary
(`pyinstaller packaging/acsdd.spec ...`) and running it with `src/` off
`PYTHONPATH` proves the frozen path still works.

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

A release only actually publishes binaries when the bump commit is tagged
`vX.Y.Z` and the tag is pushed — `.github/workflows/release.yml` triggers
on that tag pattern. An unbumped or untagged fix on `main` is invisible to
`acsdd update` and to `curl .../install.sh`, both of which only ever see
the latest GitHub release.

## Testing conventions

Tests live in `tests/`, generally one file per package (`test_capability.py`,
`test_profile.py`, `test_discovery.py`, `test_capability_generator.py`,
`test_profile_generator.py`, `test_update.py`), plain `pytest` functions (no
test classes). `test_update.py` monkeypatches `is_frozen_binary`/
`detect_platform`/`_download` rather than hitting the network or a real
binary — keep that pattern for any future changes there. A
`real_manifests_dir` fixture
(see `tests/test_capability.py`) points at the actual
`capabilities/_manifests/` example data — several tests validate against
that real data directly rather than only synthetic fixtures, so changes to
the example capabilities can break tests even if the tool code is untouched.
