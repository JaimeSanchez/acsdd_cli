---
name: capability-plan
description: Decide which ACSDD capabilities a repository should have, and update the manifests a profile change has left behind. Use when a repo has a finalized .acsdd/profiles/*.yaml but few or no capability manifests, when `acsdd capability recommend` reports gaps or stale fields, after a library upgrade or a re-run of `acsdd profile discover`, or when the user asks what capabilities their repo needs.
---

# Planning an ACSDD capability set

A finalized engineering profile describes what a repository *is*. Capability
manifests describe what an AI agent is allowed to *do* in it, and under which
constraints. `acsdd capability generate` writes a manifest, but it presumes you
already know which capabilities the repo should have. Your job is to decide
that from evidence, get the user's sign-off, and create them.

The same job runs a second time whenever the profile changes. A library upgrade
moves `technology_stack`; the manifests written against the old profile keep
asserting constraints and quality gates that are no longer true. Those fields
are exactly what an agent reads to decide how to execute a capability, so a
stale one is worse than a missing one.

**This file contains no per-capability knowledge by design.** Which capabilities
a trait implies, which existing manifests already cover them, and which fields
have gone stale all come from `acsdd capability recommend`, which is versioned
alongside the profile schema. Do not hardcode the rule table here — it would go
stale the first time the table changed.

## 1. Preconditions

```bash
acsdd --version
ls .acsdd/profiles/*.yaml acsdd/profiles/*.yaml 2>/dev/null
```

If `acsdd` is not on PATH, stop and tell the user how to install it
(`curl -fsSL https://raw.githubusercontent.com/JaimeSanchez/acsdd_cli/main/install.sh | sh`).

If there is no profile at all, stop and tell the user to run
`acsdd profile discover .` first. If the only profile is a `*-draft.yaml`, stop
and point them at the `profile-review` skill — recommendations derived from
`[REVIEW REQUIRED]` placeholders are recommendations derived from nothing.

## 2. Get the work list

```bash
acsdd capability recommend --json
```

**Never hand-derive the capability set by reading the profile YAML.** The CLI is
the source of truth for which traits imply which capabilities and for what
counts as drift. Its `--json` payload is the input to every step below.

Use the payload's `profile_path` and `capabilities_dir` wherever a later step
needs a path, rather than reconstructing one.

Per recommendation you get: `slug`, `category`, `name`, `what`, `why`,
`suggested_id`, `status` (`missing` or `covered`), `covered_by`, `triggered_by`
(the profile fields that fired the rule), `blocked_by`, `depends_on`, and a
ready-to-run `generate_command`.

Alongside them:

- `existing_by_category` — every manifest already in the repo. Coverage matching
  is keyword-based and therefore fuzzy; this is how you catch a template marked
  `missing` that an oddly-named existing manifest already covers, and the
  reverse.
- `stale` — manifest fields the profile has moved past, each with
  `manifest_value`, `profile_value`, `why`, and `fix`.
- `advisories` — never blocking. Raise them, don't act on them unprompted.

## 3. Investigate

A recommendation is a hypothesis from the profile, not a finding about the repo.
Confirm each one against the code before you propose it:

- For each `missing` entry, look for the work it describes actually happening in
  this repo. A `Create Endpoint` capability earns its place if there are
  controllers being added; it does not if the repo is a library with no HTTP
  surface. Record a `file:line` citation.
- Check `existing_in_category` for anything the keyword match got wrong in
  either direction.
- For each `blocked_by` note, find out whether the missing tooling is genuinely
  absent or just undetected. `blocked_by: no test framework is set` in a repo
  with a `tests/` directory means the profile is wrong, and the fix is
  `acsdd profile discover`, not a weaker capability.
- For each `stale` entry, open the manifest and read the procedure doc beside
  it. A major version bump often invalidates more than the version string — a
  procedure written for one ORM major may not hold on the next.
- Read the existing manifests before proposing new ones. Matching their
  granularity matters more than matching the recommended names.

## 4. Propose, then stop

Present the gaps as **one** table and wait:

| Capability | id | Category | Why it applies here | Confidence |
|------------|----|----------|--------------------|------------|

Then, as clearly separate sections:

- **Stale manifests** — one row per field, with the current value, the proposed
  value, and whether the procedure doc needs changing too.
- **Blocked** — recommendations whose gates cannot run here, and the choice they
  imply: add the tooling, or write the capability with a weaker gate.
- **Advisories** — optional, listed once, not argued for.

Order the gaps by `depends_on`: a capability its siblings depend on gets created
first, so its id is real by the time they reference it.

**Do not create or edit anything before the user responds.** Wait for explicit
sign-off. If they cut a recommendation, drop it without arguing — a capability
set nobody asked for is worse than a short one.

## 5. Apply

For each accepted gap, run its `generate_command` verbatim, then fill in what
the scaffolder left as `[REVIEW REQUIRED]`: `name`, `description`, concrete
`inputs`/`outputs` with their patterns, `adapter-id`, and the `dependencies`
entries implied by `depends_on` (each needs a `reason`). Write the procedure doc
body it wrote a stub for.

For each accepted stale field, **edit the manifest in place**, changing only
that field. Preserve key order and comments — do not reformat the file or
round-trip it through a YAML dumper.

## 6. Verify

```bash
acsdd capability validate
acsdd catalog build
```

`validate` covers schema plus cross-manifest checks — a `dependencies` entry
pointing at an id you did not actually create fails here. Re-run
`acsdd capability recommend` afterwards: the gaps you filled should come back
`covered`, and the fields you fixed should be gone from `stale`. Anything still
listed is either unfinished or was never really resolved.

## Constraints

- **Never `acsdd capability generate --force` over an existing manifest to
  "refresh" it.** That regenerates the whole file from the profile and discards
  the hand-written name, description, inputs, and outputs. Stale fields are
  edited in place, one field at a time.
- **Never propose a capability whose quality gates cannot run here without
  saying so.** A manifest asserting `test:unit-passing` in a repo with no test
  runner is a gate that silently never runs. `blocked_by` is a finding to raise
  with the user, not a footnote.
- **Stale entries are facts about the profile, not about the repo.** If the
  repo disagrees with the profile, the profile is what's wrong — re-run
  `acsdd profile discover` rather than editing manifests to match a stale
  profile.
- **Fewer, real capabilities beat a complete-looking grid.** Leaving a category
  empty is a legitimate outcome. Do not create a capability so that every
  category has one.
- Capability ids are permanent — other manifests depend on them by id, and
  `acsdd capability remove` refuses to delete one that still has dependents.
  Use the `suggested_id` from the payload; do not renumber existing manifests.
