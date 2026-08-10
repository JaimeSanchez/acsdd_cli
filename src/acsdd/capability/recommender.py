"""Recommends a capability set for a repository, and flags the manifests its
profile has outgrown.

`acsdd profile create` finalizes a profile and then points at `acsdd capability
generate --id ID --category CAT` — which presumes the author already knows
*which* capabilities their repo should have. Nothing else in the tool answers
that. This module does: a static, trait-driven rule table mapping profile
signals (an ORM is configured, a frontend exists, a test framework was
detected) to the capabilities a repo with those signals wants.

It also answers the other half, which only shows up on the second run. A
profile is not a one-time artifact — libraries get upgraded, discovery gets
re-run, `technology_stack` moves — and nothing connected that back to manifests
written against the *old* profile. A manifest keeps asserting
``doctrine/orm:^2.6`` and ``static-analysis:phpstan-level-6`` long after both
stopped being true, and those two fields are exactly what an agent reads to
decide how to execute the capability. `find_stale` is the join that catches it.

Same shape as ``profile/review.py``, and for the same reasons: a static table
plus dict probes, no detectors re-run, no repository read. It is fast,
deterministic, and testable without a repo on disk. Turning a recommendation
into an actual manifest is the caller's job (`acsdd capability generate`, a
human, or the packaged capability-plan skill).

Two rules keep the staleness half honest, because a drift report nobody trusts
is worse than none at all:

* **Unknown constraint keys are ignored.** The shipped example manifests use
  package names (``doctrine/orm:^2.6``); `capability.generator` writes profile
  *field* names (``orm:doctrine``). Both conventions coexist, and a manifest may
  carry keys from neither. Only keys this module can resolve to a current
  profile value are compared; everything else is silent.
* **Version comparison is major-component only.** ``^2.6`` vs ``2.6.4`` is not
  drift, and flagging it would train people to skip the report. ``^2.6`` vs
  ``^3.6`` is a real upgrade that invalidates a manifest's assumptions. Minor
  churn is deliberately invisible.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REVIEW_PREFIX = "[REVIEW REQUIRED"

# Leading semver range operators, stripped before comparing.
_RANGE_PREFIX_RE = re.compile(r"^[\^~>=<\s]+")
# The first version-looking token in a value. technology_stack.version arrives
# as a compound string — "6.1 (symfony/framework-bundle: 6.1.*)" — so this has
# to pick the leading number out rather than parse the whole field.
_MAJOR_RE = re.compile(r"(\d+)")

_CAP_ID_RE = re.compile(r"^([A-Z]{2,4})-([0-9]{3})$")

# Profile fields naming a *thing* (compared as strings) vs a *version*
# (compared by major component). technology_stack.version is the framework
# version, not the language's — see ProfileGenerator.generate.
_NAME_FIELDS = ("language", "framework", "orm", "database", "frontend")
_VERSION_FIELDS = ("version",)


# ---------------------------------------------------------------------
# Profile access
# ---------------------------------------------------------------------

def _get(profile: Dict, path: str):
    """Resolve a dotted path against a profile dict, returning None on any
    mismatch. Paths in the rule table are all shallow — no list indexing —
    so this stays deliberately smaller than review.py's walker."""
    current = profile
    for token in path.split("."):
        if not isinstance(current, dict) or token not in current:
            return None
        current = current[token]
    return current


def _is_set(value) -> bool:
    """True when a profile field carries a real detected value.

    A `[REVIEW REQUIRED]` placeholder is not a value: recommending the Doctrine
    capability set off ``orm: "[REVIEW REQUIRED — infer from dependencies]"``
    would be recommending it off nothing at all.
    """
    if value is None:
        return False
    text = str(value).strip()
    return bool(text) and not text.startswith(REVIEW_PREFIX)


def _resolved(profile: Dict, path: str) -> Optional[str]:
    value = _get(profile, path)
    return str(value).strip() if _is_set(value) else None


# ---------------------------------------------------------------------
# The rule table
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class GateHint:
    """A profile field whose absence weakens a capability without ruling it
    out. Missing => the recommendation still stands, carrying `note` as a
    `blocked_by` entry.

    The distinction from `requires` is the whole point: a React repo with no
    test runner should be *told* it has no test runner, not quietly served a
    shorter list of recommendations.
    """
    path: str
    note: str


@dataclass(frozen=True)
class CapabilityTemplate:
    slug: str
    category: str
    # .format()ed against the profile's stack values — see _format_context.
    name: str
    what: str
    why: str
    # Profile paths that must all carry real values, or the template is not
    # recommended at all.
    requires: Tuple[str, ...] = ()
    depends_on: Tuple[str, ...] = ()
    gate_hints: Tuple[GateHint, ...] = ()
    # Lowercase tokens; an existing manifest in the same category whose name
    # contains all of them is treated as already covering this template.
    match_keywords: Tuple[str, ...] = ()


_TEST_FRAMEWORK_HINT = GateHint(
    path="engineering_standards.test_framework",
    note=("no test framework is set in the profile — this capability's "
          "`test:unit-passing` gate has nothing to run"),
)
_STATIC_ANALYSIS_HINT = GateHint(
    path="engineering_standards.static_analysis",
    note=("no static analysis tool is set in the profile — this capability "
          "cannot carry a `static-analysis:` gate"),
)

# Trait-driven and stack-agnostic on purpose. A per-stack catalog would be
# higher quality for the stacks someone bothered to write and empty for every
# other one; keying off profile traits means a Symfony backend and a React
# frontend both get a usable answer from the same table.
_TEMPLATES: Tuple[CapabilityTemplate, ...] = (
    # --- DB: fans out from one read-only inventory capability, mirroring the
    # shape of the shipped DB-001..004 example.
    CapabilityTemplate(
        slug="db.entity-inventory",
        category="DB",
        name="Identify {orm} Entities",
        what="Introspects the entity/model mapping in the repo and diffs it "
             "against the live schema. Read-only.",
        why="Every other DB capability needs a current inventory to work from; "
            "without one they each re-scan the schema independently.",
        requires=("technology_stack.orm",),
        match_keywords=("identify",),
    ),
    CapabilityTemplate(
        slug="db.entity-create",
        category="DB",
        name="Create {orm} Entity",
        what="Adds a new entity/model with its mapping, following the "
             "conventions already present in the repo.",
        why="The most common schema change, and the one most likely to drift "
            "from existing conventions when done ad hoc.",
        requires=("technology_stack.orm",),
        depends_on=("db.entity-inventory",),
        gate_hints=(_STATIC_ANALYSIS_HINT,),
        match_keywords=("create", "entity"),
    ),
    CapabilityTemplate(
        slug="db.entity-update",
        category="DB",
        name="Update {orm} Entity",
        what="Modifies an existing entity's fields, relations, or indexes, "
             "refusing destructive changes.",
        why="Needs the inventory to validate the target exists and to diff the "
            "proposed change against current mapping.",
        requires=("technology_stack.orm",),
        depends_on=("db.entity-inventory",),
        match_keywords=("update", "entity"),
    ),
    CapabilityTemplate(
        slug="db.migration",
        category="DB",
        name="Create {orm} Migration",
        what="Generates and reviews a schema migration, checking it is "
             "reversible and non-destructive before it runs.",
        why="The step where an agent can do real damage — worth a manifest "
            "with explicit rollback and dry-run gates.",
        requires=("technology_stack.orm", "technology_stack.database"),
        depends_on=("db.entity-inventory",),
        match_keywords=("migration",),
    ),

    # --- BE
    CapabilityTemplate(
        slug="be.endpoint-create",
        category="BE",
        name="Create {framework} Endpoint",
        what="Adds a route/controller action with its request handling, "
             "serialization, and error responses.",
        why="The unit of work most backend tickets decompose into.",
        requires=("technology_stack.framework",),
        gate_hints=(_TEST_FRAMEWORK_HINT,),
        match_keywords=("endpoint",),
    ),
    CapabilityTemplate(
        slug="be.service-create",
        category="BE",
        name="Create {framework} Service",
        what="Adds a service/use-case class holding business logic, wired into "
             "the container the way the repo already does it.",
        why="Keeps logic out of controllers; the convention an agent is most "
            "likely to get wrong without a manifest to follow.",
        requires=("technology_stack.framework",),
        gate_hints=(_TEST_FRAMEWORK_HINT,),
        match_keywords=("service",),
    ),
    CapabilityTemplate(
        slug="be.input-validation",
        category="BE",
        name="Validate {framework} Request Input",
        what="Adds validation rules for user-supplied input on an existing "
             "endpoint.",
        why="Directly backs the all-inputs-validated cross-cutting constraint, "
            "which is otherwise enforced by nothing.",
        requires=("technology_stack.framework",),
        match_keywords=("validat",),
    ),

    # --- FE
    CapabilityTemplate(
        slug="fe.component-create",
        category="FE",
        name="Create {frontend} Component",
        what="Adds a UI component following the repo's existing structure, "
             "styling approach, and prop conventions.",
        why="The most frequent frontend change, and the one where convention "
            "drift is most visible.",
        requires=("technology_stack.frontend",),
        gate_hints=(_TEST_FRAMEWORK_HINT,),
        match_keywords=("component",),
    ),
    CapabilityTemplate(
        slug="fe.api-client",
        category="FE",
        name="Add {frontend} API Client Call",
        what="Wires a new backend endpoint into the frontend's data layer, "
             "with its types, error handling, and cache keys.",
        why="The seam between the two halves of the stack — where a contract "
            "change silently breaks things.",
        requires=("technology_stack.frontend",),
        match_keywords=("api",),
    ),
    CapabilityTemplate(
        slug="fe.state-slice",
        category="FE",
        name="Add {frontend} State Slice",
        what="Adds a unit of client state (store slice, context, or query) "
             "using whatever state library the repo already uses.",
        why="State management is the frontend decision most repos make "
            "inconsistently once more than one person touches it.",
        requires=("technology_stack.frontend",),
        match_keywords=("state",),
    ),

    # --- TEST
    CapabilityTemplate(
        slug="test.unit-authoring",
        category="TEST",
        name="Write Unit Tests",
        what="Adds unit tests for a target class or module, matching the "
             "repo's existing test layout and naming.",
        why="Every other capability's `test:unit-passing` gate assumes tests "
            "exist and are maintained.",
        gate_hints=(_TEST_FRAMEWORK_HINT,),
        match_keywords=("test",),
    ),
    CapabilityTemplate(
        slug="test.coverage-gate",
        category="TEST",
        name="Raise Test Coverage For A Module",
        what="Identifies uncovered paths in a target module and adds tests "
             "until the profile's min_coverage is met.",
        why="Turns a coverage number into a repeatable procedure instead of a "
            "one-off cleanup nobody repeats.",
        requires=("engineering_standards.test_framework",),
        depends_on=("test.unit-authoring",),
        match_keywords=("coverage",),
    ),

    # --- Applicable to any repo. Deliberately only two: a category left empty
    # is a better outcome than filler that makes the grid look complete.
    CapabilityTemplate(
        slug="doc.procedure-doc",
        category="DOC",
        name="Write A Capability Procedure Doc",
        what="Writes the human-readable procedure document that sits beside a "
             "capability manifest and spells out its steps.",
        why="A manifest without a procedure doc gives an agent constraints but "
            "no method.",
        match_keywords=("procedure",),
    ),
    CapabilityTemplate(
        slug="ref.safe-refactor",
        category="REF",
        name="Refactor Without Behaviour Change",
        what="Restructures existing code within the profile's per-session file "
             "and LOC limits, with tests as the invariant.",
        why="The change type an agent is most likely to let sprawl past the "
            "session limits in ai_execution_rules.",
        gate_hints=(_TEST_FRAMEWORK_HINT,),
        match_keywords=("refactor",),
    ),
)


def _format_context(profile: Dict) -> Dict[str, str]:
    """Title-cased stack values for the `name` templates. Every key a template
    can reference is always present, so .format() cannot raise; a template
    whose value is missing was filtered out by `requires` anyway."""
    stack = _get(profile, "technology_stack") or {}
    standards = _get(profile, "engineering_standards") or {}
    context = {
        "language": "the language",
        "framework": "the framework",
        "orm": "the ORM",
        "database": "the database",
        "frontend": "the frontend",
        "test_framework": "the test framework",
    }
    for key, source in (
        ("language", stack), ("framework", stack), ("orm", stack),
        ("database", stack), ("frontend", stack),
        ("test_framework", standards),
    ):
        value = source.get(key) if isinstance(source, dict) else None
        if _is_set(value):
            context[key] = str(value).strip().title()
    return context


# ---------------------------------------------------------------------
# Report objects
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class Recommendation:
    slug: str
    category: str
    name: str
    what: str
    why: str
    # None when an existing manifest already covers this — there is no id to
    # allocate, and proposing one would read as "create a second copy".
    suggested_id: Optional[str]
    status: str                       # "missing" | "covered"
    covered_by: List[str] = field(default_factory=list)
    triggered_by: List[str] = field(default_factory=list)
    blocked_by: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)

    @property
    def generate_command(self) -> Optional[str]:
        if self.suggested_id is None:
            return None
        return (f"acsdd capability generate --id {self.suggested_id} "
                f"--category {self.category}")

    def to_dict(self) -> Dict:
        return {
            "slug": self.slug,
            "category": self.category,
            "name": self.name,
            "what": self.what,
            "why": self.why,
            "suggested_id": self.suggested_id,
            "status": self.status,
            "covered_by": list(self.covered_by),
            "triggered_by": list(self.triggered_by),
            "blocked_by": list(self.blocked_by),
            "depends_on": list(self.depends_on),
            "generate_command": self.generate_command,
        }


@dataclass(frozen=True)
class StaleField:
    """A manifest field the profile has moved on from."""
    id: str
    path: str
    field: str                        # "profile_constraints" | "adapters[].stack" | "quality_gates"
    manifest_value: str
    profile_value: str
    why: str
    fix: str

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "path": self.path, "field": self.field,
            "manifest_value": self.manifest_value,
            "profile_value": self.profile_value,
            "why": self.why, "fix": self.fix,
        }


@dataclass(frozen=True)
class Advisory:
    """Worth a look, never a finding. Kept separate from `stale` so the stale
    list stays something a reader can act on without triage."""
    id: str
    path: str
    why: str

    def to_dict(self) -> Dict:
        return {"id": self.id, "path": self.path, "why": self.why}


@dataclass
class RecommendationReport:
    profile_id: Optional[str]
    recommended: List[Recommendation] = field(default_factory=list)
    existing_by_category: Dict[str, List[str]] = field(default_factory=dict)
    stale: List[StaleField] = field(default_factory=list)
    advisories: List[Advisory] = field(default_factory=list)

    @property
    def missing(self) -> List[Recommendation]:
        return [r for r in self.recommended if r.status == "missing"]

    def to_dict(self) -> Dict:
        return {
            "profile_id": self.profile_id,
            "recommended_count": len(self.recommended),
            "missing_count": len(self.missing),
            "stale_count": len(self.stale),
            "recommended": [r.to_dict() for r in self.recommended],
            "existing_by_category": {k: list(v) for k, v
                                     in self.existing_by_category.items()},
            "stale": [s.to_dict() for s in self.stale],
            "advisories": [a.to_dict() for a in self.advisories],
        }


# ---------------------------------------------------------------------
# Gap analysis
# ---------------------------------------------------------------------

# Tokens that appear in constraint ids without narrowing anything — matching a
# manifest name on "no" or "prevention" would call almost any SEC capability a
# match for almost any constraint.
_CONSTRAINT_STOPWORDS = {"no", "all", "prevention", "scan", "the", "and"}


def _constraint_keywords(cid: str) -> Tuple[str, ...]:
    """The single most distinctive token of a constraint id, for coverage
    matching. One token rather than all of them because `_covering_manifests`
    requires *every* keyword to appear, and a manifest named "Prevent SQL
    Injection" should still cover `sql-injection-prevention`."""
    tokens = [t for t in cid.split("-") if t and t not in _CONSTRAINT_STOPWORDS]
    if not tokens:
        return ()
    return (max(tokens, key=len),)


def _security_templates(profile: Dict) -> List[CapabilityTemplate]:
    """One SEC template per cross-cutting constraint the profile declares.

    Derived rather than static because the constraint list is the profile's,
    not this module's. `not-applicable` constraints are skipped — that value
    exists precisely to say "this doesn't apply here". Everything else gets a
    candidate, including the `manual` ones: a constraint with no tool behind it
    is the *strongest* argument for a written procedure, not a reason to stay
    quiet about it.
    """
    constraints = _get(profile, "security_profile.cross_cutting_constraints")
    if not isinstance(constraints, list):
        return []

    templates = []
    for entry in constraints:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("id")
        if not cid or entry.get("enforcement") == "not-applicable":
            continue

        name = entry.get("name") or str(cid).replace("-", " ").title()
        hints = ()
        if not _is_set(entry.get("tool")):
            hints = (GateHint(
                path=f"security_profile.cross_cutting_constraints[{cid}].tool",
                note=(f"no tool is set for '{cid}' — this capability can only be "
                      "a review procedure, not an automated gate"),
            ),)
        templates.append(CapabilityTemplate(
            slug=f"sec.{cid}",
            category="SEC",
            name=f"Enforce {name}",
            what=str(entry.get("rule") or f"Enforce the '{cid}' constraint.").strip(),
            why=("declared as a cross-cutting constraint in the profile, so it "
                 "applies to every change — but nothing turns it into steps"),
            gate_hints=hints,
            match_keywords=_constraint_keywords(str(cid)),
        ))
    return templates


def _next_free_id(category: str, taken: Dict[str, set]) -> str:
    used = taken.setdefault(category, set())
    number = 1
    while number in used:
        number += 1
    used.add(number)
    return f"{category}-{number:03d}"


def _covering_manifests(template: CapabilityTemplate,
                        by_category: Dict[str, List[Tuple[str, str]]]) -> List[str]:
    """Existing manifest ids in the same category whose name contains every one
    of the template's keywords.

    Matching on manifest *content* rather than any recorded identity is
    deliberate — nothing in capability.schema.json records which template a
    manifest came from, and adding a field to record it would make every
    hand-written manifest look untracked. `builder.find_doc_file` resolves
    procedure docs the same way and for the same reason. The match is fuzzy, so
    the report also carries `existing_by_category` for the reader to overrule it.
    """
    if not template.match_keywords:
        return []
    return [
        cap_id
        for cap_id, name in by_category.get(template.category, [])
        if all(keyword in name for keyword in template.match_keywords)
    ]


# ---------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class _Current:
    value: str
    # "name" -> compare as a string; "version" -> compare major component only.
    mode: str
    source: str


def _major(value: str) -> Optional[str]:
    stripped = _RANGE_PREFIX_RE.sub("", str(value).strip())
    match = _MAJOR_RE.search(stripped)
    return match.group(1) if match else None


def _leading_version(value: str) -> str:
    """The version at the front of a field, dropping any trailing gloss.

    `profile discover` writes technology_stack.version as a compound string —
    ``"6.1 (symfony/framework-bundle: 6.1.*)"`` — which is readable in the
    profile and unreadable when echoed back inside a drift finding.
    """
    return str(value).strip().split(" ", 1)[0].strip() or str(value).strip()


def _current_values(profile: Dict) -> Dict[str, _Current]:
    """The profile's current answer for every constraint key it can speak to.

    Three sources, in precedence order — an earlier one is never overwritten by
    a later:

    1. `capability.generator`'s field-name keys (``orm:doctrine``), which is
       what a scaffolded manifest carries.
    2. Package names from `technology_stack.additional` (``doctrine/orm:2.7.5``),
       which is what the hand-written example manifests carry. These come from
       composer.lock, so they are resolved versions rather than ranges.
    3. Nothing else. A key absent from this map is never compared.
    """
    stack = _get(profile, "technology_stack") or {}
    current: Dict[str, _Current] = {}

    for name in _NAME_FIELDS:
        value = stack.get(name)
        if _is_set(value):
            current[name] = _Current(str(value).strip(), "name",
                                     f"technology_stack.{name}")
    for name in _VERSION_FIELDS:
        value = stack.get(name)
        if _is_set(value):
            current[name] = _Current(_leading_version(str(value)), "version",
                                     f"technology_stack.{name}")

    for index, item in enumerate(stack.get("additional") or []):
        if not isinstance(item, dict):
            continue
        pkg, version = item.get("name"), item.get("version")
        if _is_set(pkg) and _is_set(version):
            current.setdefault(str(pkg).strip(), _Current(
                str(version).strip(), "version",
                f"technology_stack.additional[{index}].version"))

    return current


def _framework_fallback(key: str, profile: Dict,
                        current: Dict[str, _Current]) -> Optional[_Current]:
    """Resolve a package key against the framework version when the package
    name contains the framework name — ``symfony/framework-bundle`` against a
    profile whose framework is ``symfony``.

    A heuristic, and the only one here: framework components are conventionally
    released in lockstep with the framework (``symfony/*``, ``laravel/*``,
    ``@nestjs/*``). It is safe enough because it can only ever produce a
    *major*-version finding, which for a framework is a real migration and
    never a coincidence. Tried last, so an exact key always wins.
    """
    framework = current.get("framework")
    version = current.get("version")
    if framework is None or version is None:
        return None
    if framework.value.lower() not in key.lower():
        return None
    return _Current(version.value, "version",
                    f"technology_stack.version (framework {framework.value})")


def _prefix_key(entry: str) -> Optional[Tuple[str, str]]:
    """Split a ``"<key>:<value>"`` constraint or gate. Returns None for an
    entry with no colon — a bare gate like ``test:unit-passing`` splits fine,
    but ``architecture-review`` has nothing to compare."""
    if ":" not in str(entry):
        return None
    key, _, value = str(entry).partition(":")
    key, value = key.strip(), value.strip()
    return (key, value) if key and value else None


def _same_tool(declared: str, expected: str) -> bool:
    """Whether two gate values name the same tool.

    Prefix-equality rather than string equality, because a manifest routinely
    pins a configuration the profile only names: ``static-analysis:phpstan``
    in the profile against ``static-analysis:phpstan-level-6`` in the manifest
    is the same tool, and calling that drift would bury the case that matters
    (``phpstan`` vs ``psalm``).
    """
    left, right = declared.strip().lower(), expected.strip().lower()
    return left.startswith(right) or right.startswith(left)


def find_stale(profile: Dict, manifests: List[Tuple[Path, Dict]]
               ) -> Tuple[List[StaleField], List[Advisory]]:
    """Manifest fields that disagree with the profile as it stands today."""
    current = _current_values(profile)
    cap_config = _get(profile, "capability_configuration") or {}
    profile_gates = _get(profile, "quality_gates.automatic") or []
    stale: List[StaleField] = []
    advisories: List[Advisory] = []

    for path, raw in manifests:
        cap = (raw or {}).get("capability")
        if not isinstance(cap, dict):
            continue
        cap_id = str(cap.get("id") or path.stem)

        # --- adapters[].stack
        adapter_key = ("frontend_adapter" if cap.get("category") == "FE"
                       else "default_adapter")
        expected = cap_config.get(adapter_key)
        if _is_set(expected):
            declared = [str(a.get("stack")) for a in (cap.get("adapters") or [])
                        if isinstance(a, dict) and a.get("stack")]
            if declared and str(expected) not in declared:
                stale.append(StaleField(
                    id=cap_id, path=str(path), field="adapters[].stack",
                    manifest_value=", ".join(declared), profile_value=str(expected),
                    why=(f"the profile's capability_configuration.{adapter_key} is "
                         f"'{expected}', which no adapter in this manifest targets"),
                    fix=("add an adapter entry for the profile's stack, or confirm "
                         "this capability is deliberately scoped to the old one"),
                ))

        # --- profile_constraints
        for entry in cap.get("profile_constraints") or []:
            parsed = _prefix_key(entry)
            if parsed is None:
                continue
            key, manifest_value = parsed
            expected_value = current.get(key) or _framework_fallback(key, profile, current)
            if expected_value is None:
                continue  # key the profile can't speak to — never guess

            if expected_value.mode == "name":
                if manifest_value.lower() == expected_value.value.lower():
                    continue
                why = (f"the profile's {expected_value.source} is "
                       f"'{expected_value.value}'")
            else:
                manifest_major, profile_major = _major(manifest_value), _major(expected_value.value)
                if manifest_major is None or profile_major is None:
                    continue
                if manifest_major == profile_major:
                    continue  # minor drift is not drift
                why = (f"the profile's {expected_value.source} is on major version "
                       f"{profile_major} ('{expected_value.value}'); this manifest "
                       f"still assumes {manifest_major}")

            stale.append(StaleField(
                id=cap_id, path=str(path), field="profile_constraints",
                manifest_value=f"{key}:{manifest_value}",
                profile_value=f"{key}:{expected_value.value}",
                why=why,
                fix=(f"update the '{key}' constraint, and re-check that the "
                     "capability's procedure still holds on the newer version"),
            ))

        # --- quality_gates
        manifest_gates = [g for g in (cap.get("quality_gates") or [])]
        manifest_by_prefix = {}
        for gate in manifest_gates:
            parsed = _prefix_key(gate)
            if parsed:
                manifest_by_prefix.setdefault(parsed[0], parsed[1])

        for gate in profile_gates:
            parsed = _prefix_key(gate)
            if parsed is None:
                continue
            prefix, expected_tool = parsed
            declared_tool = manifest_by_prefix.get(prefix)
            if declared_tool is None:
                # Absence is an authoring choice, not drift: DB-004 drops the
                # static-analysis gate deliberately. Never a finding.
                # Phrased without naming the manifest so the CLI can group the
                # identical advisory across every manifest that has it.
                advisories.append(Advisory(
                    id=cap_id, path=str(path),
                    why=(f"the profile runs '{gate}' but no '{prefix}:' gate is "
                         "declared — intentional, or written before the tool "
                         "was adopted?"),
                ))
                continue
            if not _same_tool(declared_tool, expected_tool):
                stale.append(StaleField(
                    id=cap_id, path=str(path), field="quality_gates",
                    manifest_value=f"{prefix}:{declared_tool}",
                    profile_value=gate,
                    why=(f"the profile's automatic gate for '{prefix}' is now "
                         f"'{expected_tool}'; this manifest still names "
                         f"'{declared_tool}'"),
                    fix=("point the gate at the tool the repo actually runs — a "
                         "gate naming absent tooling cannot pass"),
                ))

    return stale, advisories


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------

def recommend(profile: Dict, manifests: List[Tuple[Path, Dict]]
              ) -> RecommendationReport:
    """profile: the ``profile:`` sub-dict of a profile YAML, matching
    `capability.generator.scaffold_manifest`'s convention. manifests: pairs as
    produced by `capability.loader.iter_manifests`. Touches no filesystem."""
    manifests = list(manifests)
    context = _format_context(profile)

    by_category: Dict[str, List[Tuple[str, str]]] = {}
    existing_by_category: Dict[str, List[str]] = {}
    taken: Dict[str, set] = {}
    for _path, raw in manifests:
        cap = (raw or {}).get("capability")
        if not isinstance(cap, dict):
            continue
        cap_id = str(cap.get("id") or "")
        category = str(cap.get("category") or "")
        name = str(cap.get("name") or "")
        by_category.setdefault(category, []).append((cap_id, name.lower()))
        existing_by_category.setdefault(category, []).append(f"{cap_id}: {name}")
        match = _CAP_ID_RE.match(cap_id)
        if match:
            taken.setdefault(match.group(1), set()).add(int(match.group(2)))

    recommended: List[Recommendation] = []
    for template in list(_TEMPLATES) + _security_templates(profile):
        triggered_by = []
        satisfied = True
        for path in template.requires:
            value = _resolved(profile, path)
            if value is None:
                satisfied = False
                break
            triggered_by.append(f"{path}={value}")
        if not satisfied:
            continue

        blocked_by = [hint.note for hint in template.gate_hints
                      if _resolved(profile, hint.path) is None]
        covered_by = _covering_manifests(template, by_category)

        recommended.append(Recommendation(
            slug=template.slug,
            category=template.category,
            name=template.name.format(**context),
            what=template.what,
            why=template.why,
            # Allocated only for gaps, and allocated as we go, so two missing
            # templates in the same category never propose the same number.
            suggested_id=None if covered_by else _next_free_id(template.category, taken),
            status="covered" if covered_by else "missing",
            covered_by=covered_by,
            triggered_by=triggered_by,
            blocked_by=blocked_by,
            depends_on=list(template.depends_on),
        ))

    stale, advisories = find_stale(profile, manifests)
    return RecommendationReport(
        profile_id=(_get(profile, "meta.id") or None),
        recommended=recommended,
        existing_by_category=existing_by_category,
        stale=stale,
        advisories=advisories,
    )
