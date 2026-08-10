# acsdd — ACSDD Framework CLI

A command-line tool for working with the ACSDD (AI-Collaborative Software
Development & Delivery) framework: capability manifests, the capability
catalog, and repository engineering profiles.

This package ships both the **tool** (`src/acsdd/`) and a working example
of the **data it operates on** (`.acsdd/capabilities/`) — four real capabilities
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

## What acsdd writes into your repo

Everything lives under a single `.acsdd/` directory:

```
your-repo/
├── .acsdd/
│   ├── profiles/           # engineering profile (draft + finalized), discovery report
│   └── capabilities/       # capability manifests, CATALOG.md, procedure docs
└── .claude/skills/         # only if you run `acsdd skill install`
```

`.claude/` is deliberately outside it — that path is
[Claude Code](https://claude.com/claude-code)'s convention, not acsdd's, and
nothing there is installed unless you ask for it.

Commit `.acsdd/`. It's hidden to stay out of your source tree's way, not
because it's disposable: the profile and manifests are what the whole
workflow reads.

**Onboarded before v0.7.0?** Your repo has a top-level `acsdd/profiles/` and
`capabilities/` instead. Both are still found — acsdd falls back to them and
prints a one-line note — but new output always goes to `.acsdd/`. To move:

```bash
mkdir -p .acsdd
mv acsdd/profiles .acsdd/profiles && rmdir acsdd
mv capabilities .acsdd/capabilities
```

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
This writes three files under `./.acsdd/profiles/`: `my-project-draft.yaml`
(the profile itself), a discovery report, and a recommendations doc.
Detection is best-effort — it leaves a `[REVIEW REQUIRED]` placeholder
wherever it couldn't determine something (an unusual stack, a database engine
with no connection string to find, a security tool it makes no attempt to
detect). Step 4 is about resolving those.

**3. Validate the profile against the schema:**

```bash
acsdd profile validate
```

PROFILE_PATH is optional too — same auto-detection under `./.acsdd/profiles`
as `capability generate`. This only checks structural shape — it passes
just as happily on a draft still full of `[REVIEW REQUIRED]` placeholders as
it does on a fully-reviewed one. It won't tell you whether you're actually
done reviewing; that's what the next step is for.

**4. Resolve the `[REVIEW REQUIRED]` fields.** `validate` above tells you the
draft is well-formed; this tells you what's still missing from it and how to
fill it in:

```bash
acsdd profile review
```

For each unresolved field it prints what the field means, what discovery
already tried and why it came up empty, which files in your repo to look at
(marked found/missing), example or schema-allowed values, and whether to edit
the draft or re-run discovery. It's informational, not a gate — it exits 0
even when it finds work to do, so wire `acsdd profile validate --strict` into
CI, never this.

If you use Claude Code, you can hand the whole step over:

```bash
acsdd skill install profile-review   # writes .claude/skills/profile-review/SKILL.md
```

Then ask Claude Code to *"finish my ACSDD profile"*. It reads the same JSON
this command emits (`acsdd profile review --json`), investigates your repo,
proposes a value per field with the evidence it found, and waits for your
sign-off before touching the draft.

**5. Finalize the profile once you've resolved every placeholder:**

```bash
acsdd profile create
```

`--draft` is also optional, auto-detected the same way (specifically
looking for a `*-draft.yaml`, since finalizing already-finalized output
wouldn't make sense).

This is the actual completeness gate: it refuses to run (listing exactly
which fields) if any `[REVIEW REQUIRED]` placeholder remains anywhere in the
profile. Once it's clean, it writes the finalized profile as
`./.acsdd/profiles/my-project.yaml` (`status: active`, version bumped to
`1.0.0`) — this is the file to point at everything downstream from here.

**6. Ask which capabilities this profile implies:**

```bash
acsdd capability recommend
```

A capability is an `AI-Collaborative` "unit of work" your repo supports, like
"run database migrations" or "generate an API controller" — and picking the
right set is the step nothing else in the tool helped with. This maps the
profile's traits (an ORM is configured, a frontend exists, a test framework
was detected) onto the capabilities a repo with those traits wants, marks each
as already covered or still missing, and hands you a ready-to-run `generate`
command with an unused id for each gap. Informational, not a gate: it exits 0
whatever it finds.

**Re-run it after any profile change** — it is also the drift check. See
[Keeping capabilities in step with the profile](#keeping-capabilities-in-step-with-the-profile)
below.

If you use Claude Code, `acsdd skill install` also ships a `capability-plan`
skill for this step: it reads `acsdd capability recommend --json`, checks each
recommendation against your actual code, and proposes a set for you to sign off
on before anything is created.

**7. Generate a capability manifest** — once per capability you accepted. Pick
an id (`<2-4 letter category>-<3 digits>`, e.g. `BE-001`) and a category
(`PLAN`, `PROFILE`, `DB`, `BE`, `FE`, `TEST`, `DOC`, `DEVOPS`, `SEC`, `REF`),
or just run the command `recommend` printed:

```bash
acsdd capability generate --id BE-001 --category BE
```

`--profile` is optional here too — omit it and acsdd looks under
`./.acsdd/profiles` for you, preferring the finalized profile over a draft
if both exist (pass `--profile PATH` explicitly if you have more than one
profile there and need to pick). This auto-creates `.acsdd/capabilities/_manifests/`
(and a matching procedure-doc folder) the first time you run it, pre-fills
everything derivable from the profile (adapter stack, profile constraints,
quality gates), and leaves the parts that require actual knowledge of the
specific capability — name, description, concrete inputs/outputs — as
`[REVIEW REQUIRED]` placeholders. Fill those in yourself, or hand the draft
manifest + your codebase to an AI coding agent and ask it to complete them.

**8. Validate the manifest, then rebuild the catalog:**

```bash
acsdd capability validate
acsdd catalog build
```

`acsdd catalog build` regenerates `.acsdd/capabilities/CATALOG.md` from every
manifest under `_manifests/` — repeat steps 7-8 for each new capability, and
re-run `catalog build` any time you add, version, or deprecate one. Wire
`acsdd catalog verify` into CI (see [`acsdd catalog`](#acsdd-catalog) below)
so a stale catalog or an invalid manifest fails the build automatically.

## Keeping capabilities in step with the profile

A profile is not a one-time artifact. You upgrade a library, re-run
`acsdd profile discover`, and `technology_stack` moves — but the manifests
written against the *old* profile keep asserting the old constraints and the
old quality gates. Those two fields are exactly what an AI agent reads to
decide how to execute a capability, so a manifest still pinning
`doctrine/orm:^2.6` on a Doctrine 3 codebase is worse than no manifest at all.

`acsdd capability recommend` is the join that catches it. Run it after any
profile change:

```bash
acsdd profile discover .      # picks up the upgrade
acsdd capability recommend    # gaps *and* drift
```

It reports two kinds of finding. **Stale fields** are manifest values the
profile has moved past — a `profile_constraints` entry on a superseded major
version, an `adapters[].stack` the profile no longer targets, a quality gate
naming a tool the repo replaced. **Advisories** are never blocking: a gate the
profile runs that a manifest doesn't declare is usually a deliberate authoring
choice, so it is raised and never treated as drift.

Drift detection is deliberately quiet where it can't be certain. It compares
only constraint keys the profile can actually speak to (both the
`orm:doctrine` convention `capability generate` writes and the
`doctrine/orm:^2.6` package convention hand-written manifests use), and it
compares versions by **major component only** — `^2.6` against `2.6.4` is not
drift, `^2.6` against `^3.6` is. A report that cries wolf on an unchanged repo
is one nobody reads.

Fix stale fields by **editing the manifest in place**. Never re-run
`capability generate --force` to "refresh" it: that regenerates the whole file
from the profile and discards the hand-written name, description, inputs, and
outputs.

## Commands

### `acsdd capability`

```bash
acsdd capability validate                 # validate every manifest in ./.acsdd/capabilities/_manifests
acsdd capability validate path/to/X.yaml  # validate a single manifest
acsdd capability list                     # table of every capability found
acsdd capability list --category DB       # filtered
acsdd capability show DB-004              # full manifest + resolved dependency chain
acsdd capability generate --id BE-005 --category BE   # --profile defaults to ./.acsdd/profiles
acsdd capability generate --profile P --id BE-005 --category BE  # or set it explicitly
acsdd capability recommend                # which capabilities this profile implies + drift
acsdd capability recommend --json         # same, machine-readable
acsdd capability remove BE-005            # list what would go (nothing is deleted)
acsdd capability remove BE-005 --force    # actually delete it
```

`validate` checks two things:
1. **Schema conformance** — against the Appendix A JSON Schema
   (`src/acsdd/schemas/capability.schema.json`, transcribed verbatim from
   the ACSDD spec).
2. **Cross-manifest integrity** — every `dependencies[].capability`
   reference actually resolves to a manifest in the set, no capability
   depends on itself, and there are no circular dependency chains.

`recommend` is the step before `generate`: it answers *which* capabilities this
repo should have, which `generate` presumes you already know. It also reports
manifests the profile has outgrown — see
[Keeping capabilities in step with the profile](#keeping-capabilities-in-step-with-the-profile).
Like `profile review`, it is informational and always exits 0; don't wire it
into CI as a gate.

`remove` is the inverse of `generate`: it deletes both files that command
wrote — the manifest and the procedure doc — and then regenerates `CATALOG.md`
so the catalog doesn't silently go stale (`--no-catalog` skips that). Without
`--force` it only *lists* what it would delete. It refuses outright, `--force`
or not, if any other capability depends on the one you're removing: that would
leave dangling references that `capability validate` and `catalog verify` both
fail on. Remove or update the dependents first.

### `acsdd catalog`

```bash
acsdd catalog build     # regenerate .acsdd/capabilities/CATALOG.md from the manifests
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
acsdd profile validate                                       # PROFILE_PATH defaults to ./.acsdd/profiles
acsdd profile validate .acsdd/profiles/my-project-draft.yaml  # or set it explicitly
acsdd profile create                                         # --draft defaults to ./.acsdd/profiles
acsdd profile create --draft .acsdd/profiles/my-project-draft.yaml  # or set it explicitly
acsdd profile review                                         # what's still [REVIEW REQUIRED], and how to resolve it
acsdd profile review --json --repo-path .                    # same report, machine-readable
acsdd profile remove my-project                              # list what would go (nothing is deleted)
acsdd profile remove my-project --force                      # actually delete it
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

`review` is the half neither of those provides: for every unresolved field it
explains what the field means, what discovery already tried and why it missed,
which files to inspect (annotated found/missing against `--repo-path`), example
or schema-allowed values, any sibling field that has to move with it, and
whether to edit the draft or re-run discovery. It also flags fields carrying no
placeholder that are still at a discovery default — a `min_coverage` of `0`
means no coverage gate at all.

**`review` is informational, not a gate.** Unresolved fields are its expected
input, so it exits 0 when it finds them; exit 1 is reserved for real failures
(no profile found, unparseable YAML). Use `acsdd profile validate --strict` in
CI — never `review`. `PROFILE_PATH` auto-detection prefers a *draft*, the
opposite of `validate`'s preference, since a finalized profile has nothing left
to review.

`remove` deletes every artifact belonging to one profile id — the draft, the
finalized profile, and both discovery markdown reports — after listing them.
Unlike the other profile commands it won't auto-detect which profile you mean:
the id is required, because guessing wrong here costs you files rather than an
error message.

### `acsdd skill`

```bash
acsdd skill list                    # shipped skills + whether each is installed here
acsdd skill install                 # install all of them into ./.claude/skills/
acsdd skill install profile-review  # or just one
acsdd skill install --dir /path/to/repo --force
acsdd skill show profile-review     # dump the markdown to stdout
acsdd skill remove profile-review --force   # uninstall it again
```

Installs the [Claude Code](https://claude.com/claude-code) skills acsdd ships
into a repository. Currently two, one per judgement-heavy step of the workflow:

- **`profile-review`** — resolves a draft profile's `[REVIEW REQUIRED]` fields
  by investigating the repo, proposing a value per field with evidence, waiting
  for your sign-off, then finalizing.
- **`capability-plan`** — decides which capabilities the finished profile
  implies, checks each candidate against your actual code, and updates the
  manifests a profile change has left stale.

Both drive themselves off the matching command's `--json` output
(`acsdd profile review --json`, `acsdd capability recommend --json`) rather than
restating what the CLI knows, so neither goes stale when detection changes.

Installing is explicit and opt-in — `profile discover` never writes outside
`./.acsdd/profiles`, and `.claude/` belongs to a different tool and is often
hand-edited, so an existing file is never overwritten without `--force`.

`remove` takes a skill name (never "all of them" by default, unlike `install`)
and cleans up the emptied `.claude/skills/<name>/` directory, but leaves
`.claude/skills/` itself alone — other tools keep their skills there too.

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
│   ├── skills.py                  # installs the shipped Claude Code skills
│   ├── schemas/                   # Appendix A & B JSON Schemas
│   ├── assets/                    # files installed into a consumer repo (skills)
│   ├── capability/                # manifest loading + validation + generation + recommendation
│   ├── catalog/                   # CATALOG.md generation
│   └── profile/                   # repo discovery + profile review/validation/finalization
├── tests/                         # pytest suite
└── .acsdd/capabilities/           # example data: 4 real capability manifests + docs
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
