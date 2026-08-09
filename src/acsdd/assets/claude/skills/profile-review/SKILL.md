---
name: profile-review
description: Resolve the [REVIEW REQUIRED] placeholders in an acsdd draft engineering profile. Use when a repo has an .acsdd/profiles/*-draft.yaml with unresolved fields, when `acsdd profile create` refuses to finalize, or when the user asks to finish, complete, or finalize their ACSDD profile.
---

# Resolving an ACSDD draft profile

`acsdd profile discover` scans a repository and writes a draft engineering
profile, leaving a `[REVIEW REQUIRED]` placeholder wherever detection came up
empty. `acsdd profile create` refuses to finalize the draft until every one of
them is resolved. Your job is to resolve them from actual evidence in the
repository, get the user's sign-off, and finalize.

**This file contains no per-field knowledge by design.** What each field means,
what discovery already tried, and where to look all come from `acsdd profile
review`, which is versioned alongside the detectors. Do not hardcode
field-specific advice here — it would go stale the first time a detector
changes.

## 1. Preconditions

```bash
acsdd --version
ls .acsdd/profiles/*-draft.yaml acsdd/profiles/*-draft.yaml 2>/dev/null
```

If `acsdd` is not on PATH, stop and tell the user how to install it
(`curl -fsSL https://raw.githubusercontent.com/JaimeSanchez/acsdd_cli/main/install.sh | sh`).

If there is no draft, stop and tell the user to run `acsdd profile discover .`
first. Do not invent a profile.

## 2. Get the work list

```bash
acsdd profile review --json --repo-path .
```

**Never hand-scan the YAML for placeholders.** The CLI is the source of truth
for what counts as unresolved; the same function backs `profile validate
--strict` and `profile create`'s gate, so anything you find by eye that it
didn't report is not actually blocking, and anything it reported that you skip
will block finalization later.

The payload's top-level `profile_path` is the draft acsdd resolved — use that
string wherever a later step needs the file, rather than reconstructing a path
yourself. Per field it gives you: `path`, the literal `placeholder`,
`what` the field means, `detection_attempted` (what discovery tried and why it
missed), `evidence` hints each marked `found: true|false|null`,
`allowed_values` / `examples`, `resolution`, and `linked_fields`.

If `unresolved_count` is 0, skip to step 6.

Also read `advisories` — fields carrying no placeholder that are still sitting
at a discovery default (a `min_coverage` of 0 means no coverage gate at all).
Raise them with the user, but never block on them.

## 3. Investigate

For each unresolved field:

- Start with the `evidence` hints marked `found: true` — those files exist.
  Hints with `found: false` are absent; `null` means the hint is prose, not a
  path.
- Read the sibling `*-discovery-report.md` and `*-recommendations.md` next to
  the draft. They record what discovery saw.
- Cross-reference fields already resolved in the draft. The ORM named in
  `technology_stack.orm` is usually the answer for
  `sql-injection-prevention`; the template engine implies the XSS answer.
- Record a concrete `file:line` citation for every value you propose.

If `has_guidance` is `false`, no guidance is registered for that path — fall
back to the raw `placeholder` string, which usually names what was attempted.

## 4. Propose, then stop

Present **one** table and wait:

| Field | Proposed value | Evidence | Confidence |
|-------|----------------|----------|------------|

- Entries with `resolution: "rerun-discovery"` are measured values, not
  opinions. Do **not** hand-edit them. Propose running the command in the
  `action` field instead, listed separately from the editable fields.
- Include `linked_fields` as their own rows. Their `path` is already re-indexed
  to the concrete list position, so it's directly editable.
- Include any advisories as a clearly-separate optional section.

**Do not modify the draft before the user responds.** Wait for explicit
sign-off. If they correct a value, use theirs without arguing.

## 5. Apply

Edit only the values the user confirmed, plus any confirmed linked fields.
Preserve key order and any comments in the YAML — do not reformat the file or
round-trip it through a YAML dumper.

## 6. Verify

```bash
# $PROFILE = the `profile_path` from step 2's JSON payload
acsdd profile validate "$PROFILE" --strict
```

If it still fails, re-run `acsdd profile review --json` and work the remaining
fields. Loop at most twice; after that, report plainly what is still unresolved
and why you could not resolve it rather than guessing to make the gate pass.

## 7. Offer to finalize

Propose `acsdd profile create` and run it only on an explicit yes. On success,
report the written path and the next step:

```bash
acsdd capability generate --id ID --category CAT
```

## Constraints

- **Never name a tool the repo does not actually use.** A profile claiming
  `phpstan` in a repo with no phpstan produces capability manifests whose
  quality gates cannot run. This is the single most damaging mistake available
  here.
- **When there is genuinely no evidence, say so and leave the placeholder.** An
  honest unresolved field is worth more than a plausible fabrication. `none` and
  `code-review (no tooling)` are legitimate answers for several fields — prefer
  them over inventing tooling.
- **Use `allowed_values` from the payload verbatim** where present; those are
  schema enums and anything else fails validation.
- Mark low-confidence proposals as low confidence in the table. Do not round up.
