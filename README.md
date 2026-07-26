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

Binaries are built for Linux and macOS on x86_64 and arm64. Windows isn't
supported yet.

### From source (contributors / development)

```bash
pip install -e ".[dev]"
```

Requires Python 3.10+. Installs the `acsdd` command on your PATH.

## Commands

### `acsdd capability`

```bash
acsdd capability validate                 # validate every manifest in ./capabilities/_manifests
acsdd capability validate path/to/X.yaml  # validate a single manifest
acsdd capability list                     # table of every capability found
acsdd capability list --category DB       # filtered
acsdd capability show DB-004              # full manifest + resolved dependency chain
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
acsdd profile discover /path/to/repo --profile-id my-project
acsdd profile validate acsdd/profiles/my-project-draft.yaml
```

`discover` runs PROFILE-001 style repository discovery — detects the
tech stack (including separate backend/frontend detection for monorepos),
conventions, architecture pattern, and repo health, then emits a draft
Profile YAML, a discovery report, and a recommendations doc.

`validate` checks the result against the Appendix B JSON Schema
(`src/acsdd/schemas/profile.schema.json`).

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
│   ├── schemas/                   # Appendix A & B JSON Schemas
│   ├── capability/                # manifest loading + validation
│   ├── catalog/                   # CATALOG.md generation
│   └── profile/                   # repo discovery + profile validation
├── tests/                         # pytest suite (18 tests)
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
