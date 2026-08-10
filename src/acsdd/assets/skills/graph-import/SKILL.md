---
name: graph-import
description: Turn a PRD into the business layer of an ACSDD engineering knowledge graph — requirements, acceptance criteria, business rules and constraints, mapped onto the capabilities that deliver them. Use when a repo has a PRD or feature spec to plan, when `acsdd change new` has created a change with no changeset yet, when `acsdd graph apply` reports a capability gap, or when the user asks to import, refine, or graph a PRD or product requirement.
---

# Importing a PRD into the engineering knowledge graph

A PRD expresses intent. The repository expresses reality. The engineering
knowledge graph connects the two, and it is what every later artifact — the
refined spec, the impact diagram, the implementation plan — is generated from.
Your job is to read a PRD, express what it asks for as typed nodes and edges,
attach each requirement to the capability that would deliver it, get the user's
sign-off, and apply it.

**The graph is a claim about this repository, not a rendering of the PRD.** A
graph built from requirement nouns alone looks authoritative and is usually
wrong: it invents capabilities that do not exist and misses the ones that
actually have to change. Every node you add is something you are asserting is
true here.

**This file contains no vocabulary or rule table by design.** Which node types
exist, which edges may join which types, what each integrity rule costs, and
the exact changeset format all come from `acsdd graph context --json`, which is
versioned alongside the schema. Do not restate any of it here — it would go
stale the first time the vocabulary changed.

**Do not invent technical detail the PRD does not contain.** This step builds
the business layer and connects it to capabilities that already exist. Naming
the classes that will implement a requirement is a later step's job, and doing
it here produces a graph that reads as discovered fact.

## 1. Preconditions

```bash
acsdd --version
acsdd change list
```

If `acsdd` is not on PATH, stop and tell the user how to install it
(`curl -fsSL https://raw.githubusercontent.com/JaimeSanchez/acsdd_cli/main/install.sh | sh`).

If there is no open change, create one — the id namespaces every business node
you are about to write, which is what stops two features colliding on a name as
ordinary as "checkout":

```bash
acsdd change new "<the feature, in a few words>" --prd docs/prd/<file>.md
```

If there is no PRD file at all, stop and ask for one. Do not reconstruct a PRD
from a conversation and then import it as though it were a document.

## 2. Get the contract

```bash
acsdd graph context --json --for prd-import
```

**Never hand-derive the vocabulary or guess at the changeset format.** This
payload is the source of truth for both, and for what will be rejected.

Use its `graph_path`, `change.changeset_path` and the ready-to-run commands in
`changeset_format` wherever a later step needs one, rather than reconstructing
a path yourself.

What it gives you:

- `vocabulary.node_types` — every type, its `layer`, its mandatory `id_prefix`,
  whether it is `durable`, and its `required_attributes`. A node missing a
  required attribute is refused.
- `vocabulary.edge_types` — every edge type, a `reading` that spells out its
  direction as a sentence, and `pairs`, which is the complete list of node-type
  pairs that edge may join. **An edge whose pair is not in that list is
  refused.** There is no "close enough".
- `rules` — every integrity rule with its `severity`. `error` blocks the apply;
  `warning` does not; `advisory` never does. Read these before you write, not
  after acsdd refuses.
- `changeset_format` — the envelope, the operations, a worked `example`, the
  `business_id_rule`, the `edge_id_rule`, and the exact dry-run, apply and
  validate commands.
- `capabilities` — every capability manifest in the repo, with `in_graph`
  telling you whether its node already exists.
- `profile` — the agreed facts about the stack. If `usable` is `false`, the
  profile is a draft: say so and point the user at the `profile-review` skill
  before trusting anything in it.
- `subgraph` — the engineering layer as it stands, which is what you attach
  requirements to.

## 3. Read the PRD

Extract only what the document actually states. For each, record the line range
it came from — you will cite it as evidence, and a node with no citation is
flagged by the `no-evidence` rule.

- **Requirements** — a behaviour a stakeholder asked for. One requirement per
  distinct behaviour; if you cannot write a single sentence describing what
  changes for a user, it is two requirements or none.
- **Acceptance criteria** — testable conditions. Each must refine a
  requirement. A requirement with no criteria is a finding to raise, not a gap
  to fill by inventing one.
- **Business rules** — invariants that hold regardless of any one requirement.
- **Constraints** — non-functional or regulatory limits, with a `kind`.

Then, for anything the PRD leaves open, write it down. Ambiguities and missing
acceptance criteria are the most valuable output of this step, and they belong
in the proposal table, not silently resolved.

## 4. Map onto capabilities

For each requirement, find the capability in `capabilities` that would deliver
it. This is the step that makes the graph worth building, and the one most
easily faked.

- Read the capability's `description`, not just its `name`. A name match that
  the description contradicts is not a match.
- A requirement may need more than one capability. Say so with more than one
  `delivered_by` edge.
- **A requirement matching nothing is the most interesting row on the table.**
  It is a capability gap: the repo cannot deliver it yet. Report it as a gap
  and run `acsdd capability recommend` to see what the profile implies. **Do
  not create a Capability node for a capability that does not exist** — the
  `capability-not-in-catalog` warning exists to catch exactly that, and
  inventing one to make the graph look complete is the failure this whole step
  is designed to prevent.
- If a capability node is missing from the graph (`in_graph: false`) but its
  manifest exists, add the node. Its `category` attribute is required.

## 5. Propose, then stop

Present **one** table and wait:

| # | Type | Node id | Name | Delivered by | PRD lines | Confidence |
|---|------|---------|------|--------------|-----------|------------|

Then, as clearly separate sections:

- **Capability gaps** — requirements nothing in this repo can deliver, and what
  `acsdd capability recommend` says about each.
- **Ambiguities** — what the PRD leaves undecided, phrased as a question with
  the line it arises from. "Defines retry behaviour but not a retry limit" is a
  finding; picking a limit yourself is not.
- **Requirements with no acceptance criteria** — listed, never invented.
- **Contradictions** — where the PRD asks for something the profile or an
  existing capability rules out. Cite both sides.

Mark low-confidence rows as low confidence. Do not round up.

**Do not write the changeset before the user responds.** Wait for explicit
sign-off. If they cut a requirement or correct a mapping, use theirs without
arguing — a graph nobody agreed to is worse than a small one.

## 6. Write the changeset

Write the file at `changeset_format.write_to`, following its `example`.

- Every business node id starts with the prefix in
  `changeset_format.business_id_rule`.
- Every node carries `evidence` with the PRD path and the line range. Nodes
  whose `source.kind` is `agent` and that cite nothing are flagged.
- Set `confidence` honestly. Below the payload's `low_threshold` raises an
  advisory, which is the correct outcome for a genuinely uncertain mapping.
- Do **not** author edge ids. Supply `type`, `from` and `to`; acsdd derives the
  id, and a hand-built one that disagrees is replaced and reported.
- Set `base_revision` to the payload's `graph_revision`.

## 7. Verify

```bash
# Both commands are given verbatim in step 2's changeset_format payload.
acsdd graph apply <changeset_path> --dry-run --json
```

Read `blocked_reasons` and `integrity`. Fix errors in the changeset and re-run.
Loop at most twice; after that, report plainly what is still failing rather
than deleting nodes to make the gate pass.

When it comes back clean:

```bash
acsdd graph apply <changeset_path>
acsdd graph validate --change <change-id> --strict
```

`--strict` also fails on warnings. Report them; do not silence them by removing
what they point at. Then show the user what landed:

```bash
acsdd graph show --layer engineering
```

## 8. Hand off

Report the revision id that was written, the capability gaps, and the open
questions — in that order. If the import produced gaps, point at the packaged
`capability-plan` skill, which decides what to do about them.

## Constraints

- **Never invent a capability, a component, or a file path.** The graph is read
  by later steps as established fact about this repository. A capability gap
  honestly reported is worth more than a complete-looking graph, and it is the
  single most damaging mistake available here.
- **Never resolve an ambiguity by choosing.** A PRD that does not specify a
  retry limit has no retry limit; picking one and encoding it in an acceptance
  criterion launders your guess into a requirement the team never agreed to.
- **Never add technical-layer nodes from a PRD.** A document that names a
  directory is describing an implementation, not a requirement, and the
  vocabulary forbids the edge anyway. Modules and tests are discovered from the
  repository, in a later step.
- **Never use `--force` to get past a refusal.** It exists for exactly one
  thing: applying a changeset whose `base_revision` is stale because the graph
  moved underneath you. If the graph moved, re-run step 2 and rebuild the
  changeset against the current revision — that is cheap, and the alternative
  is applying a mapping that was computed against a graph that no longer
  exists. It never overrides an integrity error.
- **Fewer, real requirements beat a complete-looking decomposition.** Three
  requirements that match the PRD are better than nine that pad it out.
- Re-run the whole import when the PRD changes. Editing a changeset to match a
  revised document without re-reading the repository is how the graph stops
  being a finding and becomes decoration.
