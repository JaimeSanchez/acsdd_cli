# acsdd — ACSDD Framework CLI

A command-line tool for working with the ACSDD (AI-Collaborative Software
Development & Delivery) framework: capability manifests, the capability
catalog, and repository engineering profiles.

This package ships both the **tool** (`src/acsdd/`) and a working example
of the **data it operates on** (`capabilities/`) — four real capabilities
(`DB-001`..`DB-004`, Symfony 4.4 / Doctrine / MySQL 8) so every command
below can be run immediately against something real.

## Install

### Quick install (recommended)

No Python required — this downloads a self-contained binary from
[GitHub Releases](https://github.com/JaimeSanchez/acsdd_cli/releases) and
installs it to `~/.local/bin` (no sudo needed):

```bash
curl -fsSL https://raw.githubusercontent.com/JaimeSanchez/acsdd_cli/main/install.sh | sh
```

To pin a specific version instead of installing latest:

```bash
ACSDD_VERSION=v0.2.0 curl -fsSL https://raw.githubusercontent.com/JaimeSanchez/acsdd_cli/main/install.sh | sh
```

Binaries are built for Linux (x86_64 and arm64) and macOS (Apple Silicon /
arm64 only — no Intel Mac binary; GitHub's hosted Intel macOS runners have
become too unreliable to build on). Windows isn't supported yet. On an
unsupported combination, install from source instead (below).

To update an existing binary install to the latest release in place:

```bash
acsdd update
```

Pin a specific release the same way `install.sh` does:
`acsdd update --version v0.2.0`. This only works for the binary install —
re-running `curl ... | sh` works too, but `acsdd update` doesn't need `curl`
on your PATH. For a source install, use `pip install --upgrade` or
`git pull` instead; `acsdd update` refuses to run there (there's no single
binary file for it to replace).

### From source (contributors / development)

```bash
pip install -e ".[dev]"
```

Requires Python 3.10+. Installs the `acsdd` command on your PATH.

## Quickstart

This is the full path from "just installed" to a working ACSDD setup in your
own repository. Run every command below **from the root of your repo**
(that's what lets acsdd auto-detect paths instead of you passing them by
hand). Running bare `acsdd` (no subcommand) in a repo that hasn't finished
this sequence shows the same steps as an in-terminal checklist, so you don't
need to come back here to know what's next.

**1. Install** — see [Install](#install) above.

**2. Generate the repo's engineering profile.** This scans your repo (stack,
conventions, architecture pattern, health) and writes a draft profile —
nothing in your repo is modified:

```bash
cd /path/to/my-project
acsdd profile discover .
```

`--profile-id` is optional — omit it and acsdd uses your repo directory's
name (`my-project` here); pass `--profile-id something-else` to override it.
This writes three files under `./acsdd/profiles/`: `my-project-draft.yaml`
(the profile itself), a discovery report, and a recommendations doc.
Detection is best-effort — open the draft YAML and fill in any
`[REVIEW REQUIRED]` fields it couldn't determine on its own (e.g. an unusual
stack, or a database engine it couldn't find a connection string for)
before moving on.

**3. Validate the profile against the schema:**

```bash
acsdd profile validate
```

PROFILE_PATH is optional too — same auto-detection under `./acsdd/profiles`
as `capability generate`. This only checks structural shape — it passes
just as happily on a draft still full of `[REVIEW REQUIRED]` placeholders as
it does on a fully-reviewed one. It won't tell you whether you're actually
done reviewing; that's what the next step is for.

**4. Finalize the profile once you've resolved every placeholder:**

```bash
acsdd profile create
```

`--draft` is also optional, auto-detected the same way (specifically
looking for a `*-draft.yaml`, since finalizing already-finalized output
wouldn't make sense).

This is the actual completeness gate: it refuses to run (listing exactly
which fields) if any `[REVIEW REQUIRED]` placeholder remains anywhere in the
profile. Once it's clean, it writes the finalized profile as
`./acsdd/profiles/my-project.yaml` (`status: active`, version bumped to
`1.0.0`) — this is the file to point at everything downstream from here.

**5. Generate a capability manifest from that profile.** Do this once per
capability you want to define (an `AI-Collaborative` "unit of work" your
repo supports, like "run database migrations" or "generate an API
controller"). Pick an id (`<2-4 letter category>-<3 digits>`, e.g. `BE-001`)
and a category (`PLAN`, `PROFILE`, `DB`, `BE`, `FE`, `TEST`, `DOC`,
`DEVOPS`, `SEC`, `REF`):

```bash
acsdd capability generate --id BE-001 --category BE
```

`--profile` is optional here too — omit it and acsdd looks under
`./acsdd/profiles` for you, preferring the finalized profile over a draft
if both exist (pass `--profile PATH` explicitly if you have more than one
profile there and need to pick). This auto-creates `capabilities/_manifests/`
(and a matching procedure-doc folder) the first time you run it, pre-fills
everything derivable from the profile (adapter stack, profile constraints,
quality gates), and leaves the parts that require actual knowledge of the
specific capability — name, description, concrete inputs/outputs — as
`[REVIEW REQUIRED]` placeholders. Fill those in yourself, or hand the draft
manifest + your codebase to an AI coding agent and ask it to complete them.

**6. Validate the manifest, then rebuild the catalog:**

```bash
acsdd capability validate
acsdd catalog build
```

`acsdd catalog build` regenerates `capabilities/CATALOG.md` from every
manifest under `_manifests/` — repeat steps 5-6 for each new capability, and
re-run `catalog build` any time you add, version, or deprecate one. Wire
`acsdd catalog verify` into CI (see [`acsdd catalog`](#acsdd-catalog) below)
so a stale catalog or an invalid manifest fails the build automatically.

## Commands

### `acsdd capability`

```bash
acsdd capability validate                 # validate every manifest in ./capabilities/_manifests
acsdd capability validate path/to/X.yaml  # validate a single manifest
acsdd capability list                     # table of every capability found
acsdd capability list --category DB       # filtered
acsdd capability show DB-004              # full manifest + resolved dependency chain
acsdd capability generate --id BE-005 --category BE   # --profile defaults to ./acsdd/profiles
acsdd capability generate --profile P --id BE-005 --category BE  # or set it explicitly
```

`validate` checks two things:
1. **Schema conformance** — against the Appendix A JSON Schema
   (`src/acsdd/schemas/capability.schema.json`, transcribed verbatim from
   the ACSDD spec).
2. **Cross-manifest integrity** — every `dependencies[].capability`
   reference actually resolves to a manifest in the set, no capability
   depends on itself, and there are no circular dependency chains.

### `acsdd catalog`

```bash
acsdd catalog build     # regenerate capabilities/CATALOG.md from the manifests
acsdd catalog verify    # exit 1 if CATALOG.md is stale or any manifest is invalid — for CI
```

`CATALOG.md` is a **generated file** — it's built from whatever manifests
exist under `_manifests/`, grouped by category, with an auto-detected link
to each capability's procedure doc and a rendered dependency graph. Don't
hand-edit it; run `acsdd catalog build` after adding, versioning, or
deprecating a manifest, and wire `acsdd catalog verify` into CI so a stale
catalog fails the build instead of silently drifting from reality.

### `acsdd profile`

```bash
acsdd profile discover /path/to/repo                        # --profile-id defaults to the dir name
acsdd profile discover /path/to/repo --profile-id my-project # or set it explicitly
acsdd profile validate                                       # PROFILE_PATH defaults to ./acsdd/profiles
acsdd profile validate acsdd/profiles/my-project-draft.yaml  # or set it explicitly
acsdd profile create                                         # --draft defaults to ./acsdd/profiles
acsdd profile create --draft acsdd/profiles/my-project-draft.yaml  # or set it explicitly
```

`discover` runs PROFILE-001 style repository discovery — detects the
tech stack (including separate backend/frontend detection for monorepos),
conventions, architecture pattern, and repo health, then emits a draft
Profile YAML, a discovery report, and a recommendations doc.

`validate` checks the result against the Appendix B JSON Schema
(`src/acsdd/schemas/profile.schema.json`) — structural shape only.

`create` is the completeness gate `validate` doesn't provide: it refuses to
run while any `[REVIEW REQUIRED]` placeholder remains anywhere in the
profile (listing exactly which fields), and otherwise writes a finalized
profile (`status: active`, version bumped) alongside the draft, ready to
hand to `capability generate`.

### `acsdd update`

```bash
acsdd update                    # update the binary install to the latest release
acsdd update --version v0.2.0   # pin a specific release
```

Only works for the standalone binary install (detects PyInstaller's
`sys.frozen` marker) — downloads the matching platform asset straight from
GitHub Releases, verifies its checksum, and atomically replaces the running
binary in place. Refuses to run on a source install; use
`pip install --upgrade` or `git pull` there instead.

## Project layout

```
acsdd-cli/
├── pyproject.toml
├── README.md
├── install.sh                     # curl-installer, see Install above
├── packaging/                     # PyInstaller entry point + spec for release binaries
├── .github/workflows/release.yml  # builds + publishes binaries on a version tag
├── src/acsdd/
│   ├── cli.py                     # click commands
│   ├── update.py                  # self-update for the standalone binary
│   ├── schemas/                   # Appendix A & B JSON Schemas
│   ├── capability/                # manifest loading + validation + generation
│   ├── catalog/                   # CATALOG.md generation
│   └── profile/                   # repo discovery + profile validation/finalization
├── tests/                         # pytest suite (51 tests)
└── capabilities/                  # example data: 4 real capability manifests + docs
    ├── CATALOG.md                 # generated — run `acsdd catalog build` to refresh
    ├── _manifests/
    │   ├── DB-001.yaml .. DB-004.yaml
    └── database/
        └── *.md                   # human/AI-readable procedure docs
```

## Building the standalone binary

Maintainers cutting a release don't need to do this by hand — pushing a
`vX.Y.Z` tag triggers `.github/workflows/release.yml`, which builds and
publishes binaries for linux/macOS (x86_64 + arm64) automatically. To build
one locally for testing:

```bash
pip install . pyinstaller
pyinstaller packaging/acsdd.spec --distpath dist --workpath build --clean
./dist/acsdd --version
```

## Running tests

```bash
pip install -e ".[dev]"
pytest
```
