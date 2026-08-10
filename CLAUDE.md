# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`acsdd` is a CLI for the ACSDD (AI-Collaborative Software Development &
Delivery) framework: capability manifests, a generated capability catalog,
repository engineering profiles, and an engineering knowledge graph tying them
to what a change actually does. The repo ships both the tool
(`src/acsdd/`) and working examples of the data it operates on
(`.acsdd/capabilities/` — four real capabilities, `DB-001`..`DB-004`, for a
Symfony 4.4 / Doctrine / MySQL 8 stack; `.acsdd/graph/` — a real graph of
acsdd's own architecture) so every command can be run immediately against
something real.

**acsdd contains no LLM and no network calls except `acsdd update`.** Every
judgement-heavy step is a packaged agent skill (`src/acsdd/assets/skills/`)
driving itself off an `acsdd ... --json` command. That split is the load-bearing
architectural decision in this repo: the Python side owns deterministic tables,
schemas, and gates; the agent owns interpretation. Don't add an LLM SDK.

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
acsdd capability recommend   # which capabilities this profile implies, + drift
acsdd capability generate --id BE-001 --category BE

# Re-run after any profile change (a library upgrade, a fresh discover): the
# same command reports manifests whose constraints/gates the profile outgrew.
acsdd capability recommend

# Install the packaged agent skills (profile-review, capability-plan,
# c4-component-diagram, graph-import) into both .agents/skills/ and
# .claude/skills/
acsdd skill install
acsdd skill install --agent claude   # or narrow it to one convention

# Planning a change against the engineering knowledge graph. acsdd never reads
# the PRD itself — `graph context` publishes the contract, an agent following
# the graph-import skill writes changeset.json, and `graph apply` is the gate.
acsdd change new "Guest checkout" --prd docs/prd/checkout.md
acsdd graph context --json --for prd-import       # what the skill consumes
# ...the agent writes .acsdd/changes/<id>/changeset.json, then:
acsdd graph apply --change guest-checkout --dry-run
acsdd graph apply --change guest-checkout
acsdd graph validate --change guest-checkout --strict

# Reading the graph. All informational, all exit 0.
acsdd graph show                                  # counts by layer and type
acsdd graph show --node cap:DB-001 --depth 2      # one node's neighbourhood
acsdd graph diff --change guest-checkout --for c4 # NEW/MODIFIED/REMOVED/RELATED
acsdd graph revisions

# Undo any of the above. Every `remove` lists what it would delete and does
# nothing without --force (see "Removal" under Architecture).
acsdd capability remove BE-001 --force
acsdd profile remove my-project --force
acsdd change remove guest-checkout --force
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
six subcommand groups — `capability`, `catalog`, `profile`, `graph`, `change`,
`skill` — plus one top-level command, `update`. Four of the groups carry a
`remove` subcommand (see **Removal** below). All are thin wrappers: they resolve
a directory, call into the matching non-CLI module below, and format output.
Keep business logic out of `cli.py`; put it in the module it belongs to so it
stays testable without invoking Click. The `graph` group holds to this
especially hard — every one of its printers lives in `graph/report.py` as a pure
`-> List[str]` function, because `cli.py` was already 1300 lines before it
arrived. If a *seventh* group lands, `cli.py` should become a package.

**`paths.py`** — the single authority on where acsdd's artifacts live inside
a consumer repo. Everything it writes goes under one hidden `.acsdd/` root
(`.acsdd/profiles/`, `.acsdd/capabilities/`, `.acsdd/graph/`,
`.acsdd/changes/`); the sole exceptions are
`.agents/skills/` and `.claude/skills/`, which are other agents' conventions and
stay put (see `skills.py`). `resolve_profiles_dir` / `resolve_capabilities_dir` each return
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
`change_artifact_paths` is the same idea for a change's three files.

`resolve_acsdd_root` / `resolve_graph_dir` / `resolve_changes_dir` return a bare
`Path`, **not** the `(Path, bool)` their two older siblings return. That
asymmetry is a decision, not an oversight: the graph postdates the move to
`.acsdd/`, so there is no legacy layout to fall back to and an `is_legacy` flag
that is structurally always `False` is a lie every caller would have to read
past. `resolve_changes_dir` derives from the *same* root `resolve_graph_dir`
found rather than running its own walk-up — two independent walks would let a
distant ancestor's `changes/` pair with the local `graph/`, which is the same
bug `resolve_capabilities_dir` documents.

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
- `recommender.py`: `recommend`/`find_stale` — backs `acsdd capability
  recommend`, which answers the question `generate` presumes you've already
  answered ("which capabilities should this repo have?") and, on every run
  after the first, which manifests the profile has outgrown. Same construction
  as `profile/review.py` and for the same reasons: a static table
  (`_TEMPLATES`, keyed on profile *traits* rather than per-stack catalogs) plus
  dict probes, no detector re-run, no filesystem access — `recommend` takes the
  profile sub-dict and `iter_manifests` output and touches nothing else.
  `requires` vs `gate_hints` is the load-bearing distinction: an unmet
  `requires` drops the template entirely, an unmet `gate_hint` keeps it and
  attaches a `blocked_by` note. A React repo with no test runner must be *told*
  it has no test runner, not quietly served a shorter list.
  **Two rules keep the drift half trustworthy, and both exist to avoid false
  positives — a report that cries wolf is one nobody reads.** Constraint keys
  the profile can't resolve are ignored outright (manifests carry both
  `orm:doctrine`, which `generator.py` writes, and `doctrine/orm:^2.6`, which
  the hand-written examples use; a key from neither is nobody's business).
  Versions compare by **major component only** — `^2.6` vs `2.6.4` is not
  drift, `^2.6` vs `^3.6` is. A gate the profile runs that a manifest omits is
  an *advisory*, never stale: `DB-004` drops the `static-analysis:` gate
  deliberately. Coverage matching is keyword-based against manifest names for
  the same reason `builder.find_doc_file` greps doc content — nothing records
  which template a manifest came from, and a schema field to record it would
  make every hand-written manifest look untracked. Because that match is fuzzy,
  the report always carries `existing_by_category` so the reader can overrule it.
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
which copies the agent skills acsdd ships into a consumer repo.
`assets/skills/<name>/SKILL.md` mirrors the destination layout so installing is
a straight copy.

**Nothing about a skill is vendor-specific, and the layout should keep saying
so.** A SKILL.md is YAML frontmatter (`name`, `description`) over markdown
procedure that shells out to `acsdd ... --json` — every agent can run that.
Only the *install path* differs, which is what `AGENT_TARGETS` holds:
`.agents/skills/` is the shared project convention (Codex, Cursor, Kimi CLI,
Gemini CLI, Copilot, OpenCode, Amp, Cline, Warp) and `.claude/skills/` is Claude
Code's own. `install`/`remove` write and clear **both** by default; `--agent
agents|claude|all` narrows it. The two copies are independent files, not a
symlink pair — symlinks are unreliable on Windows and under
`core.symlinks=false`, and the packaged asset is the source of truth for both,
so `--force` re-syncs rather than merges. Don't reintroduce a single hardcoded
destination: a skill installed for one agent and invisible to the next is the
exact failure the registry exists to prevent, and `skill list` prints a mark per
target for the same reason.

Four skills ship, one per
judgement-heavy step: `profile-review` (pairs with `profile/review.py`),
`capability-plan` (pairs with `capability/recommender.py`), `graph-import`
(pairs with `graph/context.py`), and `c4-component-diagram` (pairs with
nothing — see below). The split with those
modules is deliberate and worth preserving: **domain knowledge lives in the
Python table, procedure lives in `SKILL.md`.** Each skill drives itself off its
partner's `--json` output — `acsdd profile review --json`, `acsdd capability
recommend --json`, `acsdd graph context --json` — rather than restating
detection facts or rule tables in prose, which would go stale the first time
either changed.

`graph-import` is the sharpest case of that rule, because it has the most it
could restate: the node and edge vocabulary, the allowed-edge matrix, and
seventeen integrity rules. It restates none of them —
`test_skills.py::test_graph_import_refuses_to_carry_the_rule_tables_itself`
fails if it starts to.

`c4-component-diagram` is the one exception, and it's a considered one rather
than an oversight: its domain knowledge is C4-PlantUML's macros and the
NEW/MODIFIED/RELATED/REMOVED colour standard, which originate outside this repo
and don't move when acsdd's detectors do, so there is nothing for a Python table
to own and no `--json` command to write. It reads a finalized profile's
`technology_stack` when one exists and degrades silently to code inspection when
it doesn't — the coupling is opportunistic, never required. Don't "fix" it by
inventing a backing module; do keep it out of `capability recommend`'s way,
since capabilities describe what an agent may *do* and their dependency graph is
not a component graph. Its test in `tests/test_skills.py` therefore asserts the
`!include <C4/C4_Component>` line and the four tag names rather than a `--json`
invocation — those are the strings that would rot silently here.

A new skill needs an
entry in `SKILLS` and a tuple in `packaging/acsdd.spec`'s `datas`; the `skill`
command group is generic over `SKILLS` and `AGENT_TARGETS` and needs nothing.
`remove_skill` mirrors `install_skill` down to reporting a no-op through its
result rather than raising; it cleans up the emptied `<root>/<name>/` but never
`.agents/skills/` or `.claude/skills/` themselves, which host other tools'
skills too.

**`graph/`** — the engineering knowledge graph, backing `acsdd graph` and
`acsdd change`. A typed, versioned, provenance-carrying graph connecting
requirements → capabilities → components → modules, meant as the canonical
model that refined specs, C4 diagrams and implementation plans are *projections*
of rather than parallel sources of truth.

**acsdd interprets nothing here, and that is the whole design.** Turning a PRD
into nodes is judgement work, so it belongs to an agent following the packaged
`graph-import` skill, exactly as `profile-review` and `capability-plan` already
work. acsdd's two jobs are publishing the contract (`graph context --json`) and
being the gate (`graph apply`: schema → integrity → atomic write). That is what
makes "the graph is not arbitrary LLM output" an enforceable property instead of
a hope, and it is why there is still no LLM SDK, HTTP client, or graph library in
`pyproject.toml`. Don't add one to "make import work" — the import already works,
in the agent.

**One durable repo graph, per-change overlays.** `.acsdd/graph/graph.json` holds
durable engineering + technical knowledge; each change lives under
`.acsdd/changes/<change-id>/` (`change.json`, `changeset.json`, `applied.json`)
and carries its own business layer. Repo knowledge accumulates across features
instead of being rediscovered per PRD. There is deliberately **no `acsdd spec
import`** — a CLI verb named `import` would imply the CLI parses the PRD; the
equivalent is `change new` + the skill + `graph apply`.

- `vocabulary.py`: the closed node/edge tables and the **allowed-edge matrix**
  (`EDGE_TYPES[t].pairs`). Static data only — no I/O, no `jsonschema` import,
  because four consumers read it and none should need a validator to answer
  "what node types exist". Thirteen node types across three layers, ten edge
  types. **This is a permanent API**: node ids embed their type prefix
  (`cmp:checkout`) and land in consumers' checked-in `graph.json`, so adding a
  type is minor and renaming or removing one is major. There is no `File` type
  — `Module` plus `GraphEvidence.path` carries file-level precision at a
  fraction of the node count. Layer is *derived* from type (`layer_of`), never
  stored: a stored layer could contradict its type and would need its own rule
  to police the contradiction.
- `model.py`: `GraphNode`/`GraphEdge`/`GraphSource`/`GraphEvidence`/`Graph`,
  plus the canonical serialization. **Edge ids are derived, never authored**
  (`edge_id` → `<from>|<type>|<to>`), which buys idempotency for free, a stable
  diff across re-imports, and an id a reviewer can check against the matrix
  without a lookup. `graph.json` is checked in and reviewed in diffs, so byte
  stability is a correctness property: sorted arrays rather than id-keyed
  objects, omitted defaults, and `serialize(graph, generated_at=...)` taking
  `build_catalog_markdown`'s injectable clock verbatim.
- `changeset.py`: `apply_operations(graph, changeset) -> ApplyOutcome`, pure and
  disk-free. There is **no `update_edge`** — an edge *is* `(from, type, to)`, so
  changing its metadata is remove plus add; adding one would make edge ids
  negotiable and idempotency conditional. Operations already in effect land in
  `no_ops`, ones that can't be carried out in `refusals` — one bad line in a
  large agent-authored changeset yields a report, not a traceback.
- `validator.py`: JSON Schema only, mirroring `capability/validator.py`.
- `integrity.py`: everything a single-file schema can't express — the
  `validate_catalog` charter generalized. Seventeen rules; severity is carried
  by the **rule**, not the finding, so `graph context` can publish the table.
  `errors` block `apply`, `warnings` don't (`--strict` promotes them),
  `advisories` never affect an exit code. The `business-id-scoped-to-change`
  rule uses `RuleContext.owned_node_ids` to hold a change responsible only for
  the nodes *it* introduced — without that split, the second change applied to
  any repo fails on the first change's perfectly good ids.
- `repository.py` / `applier.py`: `remove_paths`-vs-`plan_removal` again. The
  repository performs the single primitive write; `plan_apply` works out
  everything that would happen without touching disk, which is what makes
  `--dry-run` the real thing with the write left off rather than a second code
  path that can disagree.
- `context.py`: the `graph context --json` payload, `recommender.py`'s
  construction — static tables plus dict probes, no detector re-run, no repo
  walk. Every key exists so `graph-import/SKILL.md` never restates a fact Python
  holds.
- `project_c4.py`: NEW/MODIFIED/REMOVED/RELATED are **computed from a
  changeset, never stored** — that's what stops them going stale, and it keeps
  `GraphNode.status` free for the orthogonal durable lifecycle. The
  `c4-component-diagram` skill keeps owning the PlantUML macros and colours (its
  exception above still holds); it merely gains a classification table it used
  to derive by inspection.
- `report.py`: pure `-> List[str]` renderers. `cli.py` echoes them. **`graph
  show` must never grow a capability-dependency mode** — `catalog build` already
  renders those edges, and a second renderer of the same edges would drift.

`graph apply` is **not** `--force`-gated in general: refusing to write a
validated changeset without a flag would make the subsystem unusable unattended.
`--force` there means exactly one thing — proceed past a stale `base_revision` —
and never overrides an integrity error, the same line `capability remove` draws.
`graph apply --json` is the one mutating command with a `--json` flag, because
the skill needs the new revision id and the finding lists back in one read.

The blocking gate on hallucinated content is **procedural, deliberately**:
`no-evidence` is an advisory, because a warning-blocking apply would train
people to reach for `--force` reflexively and that would defeat the
base-revision check too. The gate lives in the skill's propose-then-stop step.

`.acsdd/graph/graph.json` in this repo is real example data (acsdd's own
architecture), the same deal as the example manifests: `tests/test_graph_integrity.py`
validates it, so editing it can break tests even when no tool code moved.

**`removal.py`** — `remove_paths(paths, protect=...)`, the one deletion
primitive, shared by all four `remove` commands. *What* to delete is answered
per-artifact elsewhere (`capability.remover`, `paths.profile_artifact_paths`,
`paths.change_artifact_paths`, `skills.remove_skill`); this only unlinks and
then prunes the directories it emptied. `protect` exists because some
directories are structural rather than incidental: `_manifests/` is how
`resolve_capabilities_dir` recognizes a capabilities tree at all, and a profiles
or changes directory that vanished with its last entry would read as "never
onboarded". Pruning stops at immediate parents so emptying `backend/` can't
cascade up into the capabilities root — **don't add `rmtree` here** to "handle
directories"; that limit is the guard against the cascade.

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
four JSON Schemas, loaded via
`importlib.resources.files("acsdd.schemas")`) and `assets/` (the agent skills,
loaded via `importlib.resources.files("acsdd.assets")`). Neither is
ever addressed by a relative filesystem path or one derived from `__file__`.

**Schema conventions, and the one bounded exception.** The schemas declare no
`$id` and wrap everything in a single-key envelope (`capability:`, `profile:`,
`graph:`, `changeset:`), with the document's own version as a semver string
field inside it. `capability.schema.json` and `profile.schema.json` also inline
every sub-object rather than using `$defs`, because neither has a shape worth
naming twice. The two **graph** schemas deviate and use local `$defs`: `node`,
`edge`, `evidence` and `source` each repeat *within* one file, and inlining a
60-line object at five sites produces a document no reviewer reads, which
defeats the reason the convention exists. The exception is bounded to exactly
that — a shape repeating three or more times inside one file. Still no `$id`,
and still no cross-file `$ref`: the node/edge shapes are duplicated *between*
`engineering-graph.schema.json` and `graph-changeset.schema.json`, and
`tests/test_graph_vocabulary.py` asserts both files' enums stay identical, which
is what stops them drifting without needing a resolver registry.

**`to_dict()` is the norm; `from_dict()` is required only for persisted types.**
`recommender.py`'s dicts are terminal output and never round-trip. `graph/model.py`
is read back on every command, so its parse and serialize halves must live
adjacent or they drift within one release — and a round-trip test enforces the
pair.

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
to the CLI, to the manifest/profile/graph schemas, **or to the graph's
node/edge vocabulary**.

The vocabulary is a stronger compatibility surface than any of the schemas,
which is why it is called out separately: node ids embed their type prefix
(`cmp:checkout`) and land in consumers' checked-in `graph.json`, so adding a
node or edge type is a minor bump, but renaming or removing one invalidates
data that already exists in other people's repositories.

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
`test_profile_generator.py`, `test_profile_review.py`,
`test_capability_recommend.py`, `test_paths.py`,
`test_skills.py`, `test_update.py`, and the graph set —
`test_graph_vocabulary.py`, `test_graph_model.py`, `test_graph_changeset.py`,
`test_graph_repository.py`, `test_graph_integrity.py`, `test_graph_context.py`,
`test_graph_c4.py`, `test_graph_cli.py`), plain `pytest` functions (no
test classes). `test_update.py` monkeypatches `is_frozen_binary`/
`detect_platform`/`_download` rather than hitting the network or a real
binary — keep that pattern for any future changes there. A
`real_manifests_dir` fixture
(see `tests/test_capability.py`) points at the actual
`.acsdd/capabilities/_manifests/` example data — several tests validate against
that real data directly rather than only synthetic fixtures, so changes to
the example capabilities can break tests even if the tool code is untouched.
`.acsdd/graph/graph.json` is treated the same way by `test_graph_integrity.py`,
which asserts the shipped example graph has zero findings at every severity and
still agrees with the real manifests.

The `remove` commands have no test file of their own; each one's tests sit with
its create counterpart (`test_capability_generator.py`, `test_profile_generator.py`,
`test_skills.py`, `test_graph_cli.py`), because the useful test is the round
trip. Every no-`--force` test must assert the files are **still on disk**
afterwards, not just that the exit code was 1 — a command that prints the
refusal *and* deletes would pass the weaker assertion.

Several tests are guards rather than coverage, and shouldn't be weakened to make
a change pass:

- `test_discovery.py::test_review_covers_every_placeholder_real_discovery_emits`
  runs real discovery against the fixture repo and asserts every placeholder it
  emits has a registered entry in `review.py`. Adding an emission site to
  `_discovery_impl.py` without guidance fails here — that's the point.
- `test_profile_review.py::test_allowed_values_match_the_json_schema` reads
  `profile.schema.json` and checks the enums `review.py` keeps as plain
  constants still match it.
- `test_capability_recommend.py::test_every_template_path_resolves_against_a_real_discovered_profile`
  runs real discovery against **two** fixture repos (a PHP one and a JS one)
  and asserts every profile path `recommender.py`'s `_TEMPLATES` keys off is
  one discovery still emits. Two repos because several stack fields are
  conditionally omitted — `technology_stack.frontend` only appears once there
  is a frontend — and a single-repo version would declare those fields dead. A
  renamed profile field otherwise makes a trait silently stop firing, with
  nothing failing and the recommendations quietly getting worse.
  `test_templates_stay_consistent_with_the_schemas` is its cheaper sibling
  (categories ⊆ the schema enum, `depends_on` resolves to a real slug).
- `test_graph_vocabulary.py` mirrors `vocabulary.py` against **both** graph
  schemas — node/edge enums, the id pattern byte-for-byte, statuses and source
  kinds. It is the only thing stopping the two schema files drifting from each
  other, which is the price of not using cross-file `$ref`. It also asserts
  every node type appears somewhere in the edge matrix (a type nothing can
  connect to is a dead type) and that id prefixes are injective (or the
  `id-prefix-matches-type` rule is unenforceable).
- `test_graph_context.py::test_the_payload_advertises_every_type_and_every_rule`
  asserts `graph context --json` publishes every node type, every edge type and
  every integrity rule. A rule an agent never learns about is a rule that fires
  as a surprise at apply time — the direct analogue of the discovery guard
  above. Its sibling checks the payload's worked example is itself a valid
  changeset, since that example is what an agent copies.
- `test_graph_model.py::test_reserializing_a_parsed_graph_is_a_noop` is what
  `catalog verify` relies on for CATALOG.md, applied to `graph.json`: reading
  the file and writing it back must produce no diff.

`test_skills.py` covers the packaged-asset load path for the *source* install
only; the frozen half can't be tested from pytest and is asserted by
`release.yml`'s smoke-test step instead. It also asserts `graph-import`'s
SKILL.md does *not* enumerate the edge types — a skill that restated the matrix
would go stale silently, which is the failure the `--json` contract exists to
prevent.
