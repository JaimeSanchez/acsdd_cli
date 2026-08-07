"""Explains the [REVIEW REQUIRED] placeholders left behind by `profile discover`.

`profile validate --strict` and `profile create` both tell you *which* fields
are still unresolved — a bare list of dotted paths like
``security_profile.cross_cutting_constraints[3].tool``. Neither says what the
field means, what discovery already tried, or where in the repository to look
for the answer. On a non-PHP repo that's most of the profile: version/orm
detection is Symfony-only and three of the five cross-cutting constraint tools
are hardcoded placeholders that discovery makes no attempt to detect at all.

This module is the missing half. It is deliberately a *static* hint table plus
``Path.exists()`` probes — it does not re-run any detector, so it stays fast,
deterministic, and testable without a repository. Turning the hints into an
actual value is the caller's job (a human, or the packaged profile-review
Claude Code skill).

Two invariants keep this honest:

* Unresolved fields come from ``generator.find_unresolved_fields`` and nowhere
  else, so `validate --strict`, `create`, and `review` can never disagree about
  what "unresolved" means.
* ``guidance_for`` never returns None and never raises. `_discovery_impl` grows
  new placeholder sites over time; an unregistered path degrades to echoing the
  raw placeholder rather than crashing. `tests/test_discovery.py`'s drift test
  is what stops that fallback from quietly becoming the normal case.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from acsdd.profile._discovery_impl import ProfileGenerator
from acsdd.profile.generator import REVIEW_PREFIX, find_unresolved_fields

# Sentinel for "this path doesn't resolve in the profile dict". None is a real
# value here (capability_configuration.frontend_adapter is nullable), so it
# can't do double duty as "missing".
_MISSING = object()

_INDEX_RE = re.compile(r"\[\d+\]")
_PARENTHETICAL_RE = re.compile(r"\s*\([^)]*\)\s*$")


@dataclass(frozen=True)
class LinkedField:
    """A sibling field whose value probably has to change alongside this one."""
    path: str
    note: str
    allowed_values: Optional[List[str]] = None

    def to_dict(self) -> Dict:
        return {"path": self.path, "note": self.note,
                "allowed_values": self.allowed_values}


@dataclass(frozen=True)
class FieldGuidance:
    key: str
    what: str
    detection_attempted: str
    evidence: List[str] = field(default_factory=list)
    allowed_values: Optional[List[str]] = None
    examples: List[str] = field(default_factory=list)
    # "edit" — resolve by editing the draft in place.
    # "rerun-discovery" — a measured value; hand-editing it would be a lie.
    resolution: str = "edit"
    action: Optional[str] = None
    linked_fields: List[LinkedField] = field(default_factory=list)
    has_guidance: bool = True


@dataclass(frozen=True)
class EvidenceHint:
    hint: str
    # None when review_profile() was called without a repo_path — "we didn't
    # look", which is not the same answer as "not there".
    found: Optional[bool] = None

    def to_dict(self) -> Dict:
        return {"hint": self.hint, "found": self.found}


@dataclass(frozen=True)
class UnresolvedField:
    path: str
    placeholder: str
    # The sibling `id` when this field lives in a list of identified objects —
    # what the guidance was actually keyed on. None for plain paths.
    discriminator: Optional[str]
    guidance: FieldGuidance
    evidence: List[EvidenceHint] = field(default_factory=list)

    def to_dict(self) -> Dict:
        g = self.guidance
        return {
            "path": self.path,
            "placeholder": self.placeholder,
            "discriminator": self.discriminator,
            "has_guidance": g.has_guidance,
            "what": g.what,
            "detection_attempted": g.detection_attempted,
            "evidence": [e.to_dict() for e in self.evidence],
            "allowed_values": g.allowed_values,
            "examples": list(g.examples),
            "resolution": g.resolution,
            "action": g.action,
            "linked_fields": [lf.to_dict() for lf in self.resolved_links()],
        }

    def resolved_links(self) -> List[LinkedField]:
        """Guidance is registered against the index-normalized path, so linked
        fields carry `[]` where this field's concrete path carries `[3]`.
        Re-index them so the caller gets a path it can actually write to."""
        index = _INDEX_RE.search(self.path)
        if index is None:
            return list(self.guidance.linked_fields)
        return [
            LinkedField(lf.path.replace("[]", index.group(0)), lf.note, lf.allowed_values)
            for lf in self.guidance.linked_fields
        ]


@dataclass(frozen=True)
class Advisory:
    """A field with no placeholder that still wants a human decision.

    Only emitted while the field is untouched at its discovery default, so it
    disappears the moment someone sets it — self-extinguishing, with no state
    tracked anywhere. Never counted as unresolved and never affects exit codes:
    these are not gates.
    """
    path: str
    current_value: Any
    why: str

    def to_dict(self) -> Dict:
        return {"path": self.path, "current_value": self.current_value, "why": self.why}


@dataclass
class ReviewReport:
    profile_id: Optional[str]
    unresolved: List[UnresolvedField] = field(default_factory=list)
    advisories: List[Advisory] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "profile_id": self.profile_id,
            "unresolved_count": len(self.unresolved),
            "unresolved": [u.to_dict() for u in self.unresolved],
            "advisories": [a.to_dict() for a in self.advisories],
        }


# ---------------------------------------------------------------------
# Path handling — the inverse of find_unresolved_fields' walk
# ---------------------------------------------------------------------

def _parse_path(path: str) -> List[Union[str, int]]:
    """Splits a dotted/indexed path into lookup tokens.

    "security_profile.cross_cutting_constraints[0].tool" ->
        ["security_profile", "cross_cutting_constraints", 0, "tool"]
    """
    tokens: List[Union[str, int]] = []
    for segment in path.split("."):
        name, *indices = segment.split("[")
        if name:
            tokens.append(name)
        for raw in indices:
            tokens.append(int(raw.rstrip("]")))
    return tokens


def _get(profile: Dict, tokens: List[Union[str, int]]) -> Any:
    """Resolves parsed tokens against a profile dict, returning _MISSING rather
    than raising on any mismatch — callers get a report, not a traceback."""
    current: Any = profile
    for token in tokens:
        if isinstance(token, int):
            if not isinstance(current, list) or token >= len(current):
                return _MISSING
            current = current[token]
        else:
            if not isinstance(current, dict) or token not in current:
                return _MISSING
            current = current[token]
    return current


def _normalize(path: str) -> str:
    """Collapses list indices so guidance can be registered once per family:
    "...constraints[3].tool" -> "...constraints[].tool"."""
    return _INDEX_RE.sub("[]", path)


# ---------------------------------------------------------------------
# The guidance table
# ---------------------------------------------------------------------

# Kept in sync with the enum in schemas/profile.schema.json — see
# test_allowed_values_match_the_json_schema.
_ENFORCEMENT_VALUES = [
    "automatic", "manual", "automatic_and_manual", "advisory", "not-applicable",
]
_TECH_DEBT_VALUES = ["low", "medium", "high"]

_ENFORCEMENT_LINK = LinkedField(
    path="security_profile.cross_cutting_constraints[].enforcement",
    note=("currently \"manual\" only because no scanner was detected — set it to "
          "\"automatic\" if the tool you name actually runs in CI, or "
          "\"not-applicable\" if the constraint doesn't apply to this repo"),
    allowed_values=_ENFORCEMENT_VALUES,
)

_NO_DETECTION = ("nothing — this constraint's tool is hardcoded as a placeholder; "
                 "discovery makes no attempt to detect it")

# The five cross-cutting constraints share a path shape but need genuinely
# different advice, so they're keyed off the sibling `id` rather than the list
# index (a human reordering the list must not silently swap "pick a secret
# scanner" for "prevent XSS").
_CONSTRAINT_GUIDANCE: Dict[str, FieldGuidance] = {
    "no-hardcoded-secrets": FieldGuidance(
        key="security_profile.cross_cutting_constraints[].tool/no-hardcoded-secrets",
        what="the tool that stops credentials, API keys, and tokens reaching the repo",
        detection_attempted=("the presence of .snyk, .trivyignore, or .gitleaks.toml "
                             "at the repo root — no other secret scanner is recognized"),
        evidence=[".gitleaks.toml", ".trivyignore", ".snyk", ".pre-commit-config.yaml",
                  ".github/workflows"],
        examples=["gitleaks", "trufflehog", "detect-secrets", "git-secrets",
                  "github-secret-scanning"],
        linked_fields=[_ENFORCEMENT_LINK],
    ),
    "all-inputs-validated": FieldGuidance(
        key="security_profile.cross_cutting_constraints[].tool/all-inputs-validated",
        what="what enforces that user-supplied input is validated before use",
        detection_attempted=_NO_DETECTION,
        evidence=["the framework's own validation component (see technology_stack.framework)",
                  "request/DTO classes under src/", "any schema-validation dependency in the manifest"],
        examples=["symfony/validator", "zod", "pydantic", "joi", "class-validator",
                  "code-review (no tooling)"],
    ),
    "sql-injection-prevention": FieldGuidance(
        key="security_profile.cross_cutting_constraints[].tool/sql-injection-prevention",
        what="what keeps user input out of raw SQL string concatenation",
        detection_attempted=_NO_DETECTION,
        evidence=["technology_stack.orm — a parameterizing ORM usually IS the answer here",
                  "any raw-SQL call sites in the repo", "static-analysis rules already configured"],
        examples=["doctrine (parameterized queries)", "sqlalchemy", "prisma",
                  "phpstan", "semgrep", "code-review (no tooling)"],
    ),
    "xss-prevention": FieldGuidance(
        key="security_profile.cross_cutting_constraints[].tool/xss-prevention",
        what="what escapes or encodes output for the context it lands in",
        detection_attempted=_NO_DETECTION,
        evidence=["the template engine in use (Twig/Blade/JSX auto-escape by default)",
                  "any Content-Security-Policy configuration", "sanitizer dependencies in the manifest"],
        examples=["twig (auto-escaping)", "react (JSX escaping)", "dompurify",
                  "html-sanitizer", "code-review (no tooling)"],
    ),
    "dependency-vulnerability-scan": FieldGuidance(
        key="security_profile.cross_cutting_constraints[].tool/dependency-vulnerability-scan",
        what="what scans dependencies for known vulnerabilities",
        detection_attempted=("the same .snyk / .trivyignore / .gitleaks.toml probe as "
                             "no-hardcoded-secrets — dependabot, renovate, and the "
                             "package managers' own audit commands are not detected"),
        evidence=[".github/dependabot.yml", "renovate.json", ".snyk", ".trivyignore",
                  ".github/workflows"],
        examples=["dependabot", "renovate", "snyk", "trivy", "composer audit", "npm audit",
                  "pip-audit"],
        linked_fields=[_ENFORCEMENT_LINK],
    ),
}

_CONSTRAINT_DEFAULT = FieldGuidance(
    key="security_profile.cross_cutting_constraints[].tool",
    what="the tool or process that enforces this cross-cutting constraint",
    detection_attempted=("unknown constraint id — discovery only ever emits the five "
                         "listed in ProfileGenerator.generate()"),
    evidence=["the constraint's own `rule` field, one line below this one in the draft"],
    examples=["code-review (no tooling)"],
)

_GUIDANCE: Dict[str, FieldGuidance] = {
    "technology_stack.version": FieldGuidance(
        key="technology_stack.version",
        what="the framework/runtime version this repo actually runs on",
        detection_attempted=("the resolved version in composer.lock, falling back to the "
                             "constraint in composer.json — Symfony only, so every other "
                             "stack always lands here"),
        evidence=["composer.lock", "package.json (engines) / .nvmrc",
                  "pyproject.toml (requires-python) / .python-version",
                  "go.mod (go directive)", "Cargo.toml (rust-version)",
                  "pom.xml / build.gradle", "Dockerfile (FROM tag)",
                  ".github/workflows (setup-* action versions)"],
        examples=["6.1.11", "8.2", "20.11.0", "3.12"],
    ),
    "technology_stack.orm": FieldGuidance(
        key="technology_stack.orm",
        what="the ORM or data-access layer the app uses to reach the database",
        detection_attempted=("doctrine/orm or doctrine/doctrine-bundle in composer.json's "
                             "require block — and nothing else; no ORM detection exists "
                             "for any non-PHP stack"),
        evidence=["composer.json / package.json / pyproject.toml / go.mod dependencies",
                  "the lockfile for the detected language",
                  "entity/model class definitions under src/"],
        examples=["doctrine", "eloquent", "typeorm", "prisma", "sqlalchemy", "django-orm",
                  "gorm", "diesel", "hibernate", "none (raw SQL)"],
    ),
    "technology_stack.database": FieldGuidance(
        key="technology_stack.database",
        what="the database engine the app actually runs against",
        detection_attempted=("a DATABASE_URL=<scheme>:// line in .env, .env.dist, "
                             ".env.example, or .env.test; then `image:` lines in "
                             "docker-compose.yml/.yaml, compose.yml/.yaml, or their "
                             "override variants"),
        evidence=[".env.example", ".env.dist", "docker-compose.yml", "compose.yaml",
                  "config/packages/doctrine.yaml", "settings.py",
                  ".github/workflows (CI service containers)", "migrations/"],
        examples=["postgresql", "mysql", "mariadb", "sqlite", "sqlserver", "mongodb"],
    ),
    "technology_stack.frontend": FieldGuidance(
        key="technology_stack.frontend",
        what="the frontend framework this repo ships, if any",
        detection_attempted=("scoring package.json plus its react/react-dom/vue "
                             "dependencies; a bare package.json alone only reaches "
                             "confidence 0.4, which is below the 0.6 bar and lands here. "
                             "Check the placeholder text above — it says whether a value "
                             "was guessed at low confidence or nothing was found at all"),
        evidence=["package.json (dependencies)", "vite.config.ts", "next.config.js",
                  "angular.json", "templates/ (Twig) or resources/views/ (Blade)"],
        examples=["react", "vue", "angular", "svelte", "twig (server-rendered)", "none"],
    ),
    "engineering_standards.code_style": FieldGuidance(
        key="engineering_standards.code_style",
        what="the formatter or linter that defines this repo's code style",
        detection_attempted=("an exact filename match against a fixed list: "
                             ".php_cs.dist, .php-cs-fixer.dist.php, pyproject.toml, "
                             ".eslintrc[.js|.json], phpcs.xml[.dist], .pylintrc, "
                             "clippy.toml, checkstyle.xml"),
        evidence=[".editorconfig", ".prettierrc", "biome.json", ".pre-commit-config.yaml",
                  "Makefile / composer.json scripts", ".github/workflows (lint steps)"],
        examples=["prettier", "eslint", "black", "ruff", "php-cs-fixer", "gofmt", "rustfmt"],
    ),
    "engineering_standards.static_analysis": FieldGuidance(
        key="engineering_standards.static_analysis",
        what="the static analyser run against this codebase",
        detection_attempted=("phpstan config filenames only (phpstan.neon, "
                             "phpstan.neon.dist, phpstan.dist.neon) — mypy, psalm, tsc, "
                             "and sonar are never detected even when configured"),
        evidence=["mypy.ini / pyproject.toml [tool.mypy]", "psalm.xml",
                  "tsconfig.json (strict)", "sonar-project.properties",
                  ".github/workflows (analysis steps)"],
        examples=["phpstan", "psalm", "mypy", "tsc", "sonarqube", "none"],
    ),
    "engineering_standards.test_framework": FieldGuidance(
        key="engineering_standards.test_framework",
        what="the test runner the repo's suite is written against",
        detection_attempted=("phpunit.xml[.dist], jest.config.js, vitest.config.ts, "
                             "pytest.ini, or go.mod at the repo root — a config living "
                             "in a subdirectory is missed"),
        evidence=["tests/ or spec/", "pyproject.toml [tool.pytest.ini_options]",
                  "package.json (scripts.test)", "phpunit.xml in a subdirectory"],
        examples=["phpunit", "pytest", "jest", "vitest", "go test", "cargo test", "junit"],
    ),
    "capability_configuration.default_adapter": FieldGuidance(
        key="capability_configuration.default_adapter",
        what=("the capability adapter id new manifests inherit — a naming convention "
              "consumed by `acsdd capability generate`, not a resolved import"),
        detection_attempted=("a lookup of the detected stack in ProfileGenerator's "
                             "ADAPTER_MAP; landing here means the stack came back "
                             "'unknown' or isn't one of the mapped ids"),
        evidence=["technology_stack.language and .framework, two sections up in this draft",
                  "the discovery report's stack-detection table"],
        examples=sorted(ProfileGenerator.ADAPTER_MAP.values()),
    ),
    "capability_configuration.frontend_adapter": FieldGuidance(
        key="capability_configuration.frontend_adapter",
        what="the capability adapter id for frontend work, or null if there is no frontend",
        detection_attempted=("a lookup in FRONTEND_ADAPTER_MAP, which only knows "
                             "node-react -> react and node-vue -> vue"),
        evidence=["technology_stack.frontend, one section up in this draft",
                  "package.json (dependencies)"],
        examples=sorted(ProfileGenerator.FRONTEND_ADAPTER_MAP.values()) + ["null (no frontend)"],
    ),
    "security_profile.cross_cutting_constraints[].tool": _CONSTRAINT_DEFAULT,
    "repository_health.tech_debt_score": FieldGuidance(
        key="repository_health.tech_debt_score",
        what="a measured tech-debt band for the repo",
        detection_attempted=("nothing — discovery ran with --depth surface, which skips "
                             "the full-tree walk that computes this"),
        evidence=[],
        allowed_values=_TECH_DEBT_VALUES,
        examples=_TECH_DEBT_VALUES,
        resolution="rerun-discovery",
        action="acsdd profile discover . --depth deep --force",
    ),
}

# Not registered, deliberately: the `architecture` placeholder emitted under
# --depth surface never reaches the profile YAML (it only appears in the
# discovery report and on stdout), so find_unresolved_fields can never surface
# it here and a guidance entry for it would be dead code.


def guidance_for(path: str, profile: Dict) -> FieldGuidance:
    """Returns guidance for a concrete unresolved path. Never None, never raises.

    Lookup is index-normalized ("...constraints[3].tool" ->
    "...constraints[].tool"); for the cross-cutting constraints the entry is
    then refined by the sibling `id`, since the list index carries no meaning.
    """
    normalized = _normalize(path)
    base = _GUIDANCE.get(normalized)

    if base is _CONSTRAINT_DEFAULT:
        discriminator = _discriminator_for(path, profile)
        return _CONSTRAINT_GUIDANCE.get(discriminator, _CONSTRAINT_DEFAULT)

    if base is not None:
        return base

    placeholder = _get(profile, _parse_path(path))
    return FieldGuidance(
        key=normalized,
        what="No specific guidance is registered for this field.",
        detection_attempted=(placeholder if isinstance(placeholder, str)
                             else "unknown — the placeholder could not be read back"),
        resolution="edit",
        has_guidance=False,
    )


def _discriminator_for(path: str, profile: Dict) -> Optional[str]:
    """The `id` of the list item a path lives in, when there is one."""
    tokens = _parse_path(path)
    for cut in range(len(tokens) - 1, 0, -1):
        if isinstance(tokens[cut - 1], int):
            parent = _get(profile, tokens[:cut])
            if isinstance(parent, dict) and isinstance(parent.get("id"), str):
                return parent["id"]
            return None
    return None


# ---------------------------------------------------------------------
# Advisories
# ---------------------------------------------------------------------

# (path, discovery default, why it wants a human). Emitted only while the value
# still equals the default — see Advisory's docstring.
_ADVISORIES = [
    ("engineering_standards.min_coverage", 0,
     "discovery writes 0 whenever it can't parse a coverage report, and 0 means "
     "'no coverage gate at all' — set the number the team actually intends to hold to"),
    ("ai_execution_rules.max_files_per_session", 3,
     "a hardcoded default, not a measurement — calibrate it to how large a change "
     "your reviewers can actually absorb in one sitting"),
    ("capability_configuration.overrides", {},
     "empty means every capability inherits default_adapter verbatim; add entries here "
     "if some capabilities need a different adapter"),
]


def _advisories(profile: Dict) -> List[Advisory]:
    found = []
    for path, default, why in _ADVISORIES:
        value = _get(profile, _parse_path(path))
        if value is not _MISSING and value == default and type(value) is type(default):
            found.append(Advisory(path=path, current_value=value, why=why))
    return found


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------

def review_profile(profile: Dict, repo_path: Optional[Path] = None) -> ReviewReport:
    """Builds a guidance report for the inner profile dict (i.e. data["profile"]).

    ``repo_path``, when given, is probed with Path.exists() so each evidence
    hint can be annotated with whether that file is actually present — the
    difference between "go look at docker-compose.yml" and "there is no
    docker-compose.yml here". No file is ever read.
    """
    report = ReviewReport(profile_id=(profile.get("meta") or {}).get("id"))

    for path in find_unresolved_fields(profile):
        guidance = guidance_for(path, profile)
        value = _get(profile, _parse_path(path))
        report.unresolved.append(UnresolvedField(
            path=path,
            placeholder=value if isinstance(value, str) else REVIEW_PREFIX + "]",
            discriminator=_discriminator_for(path, profile),
            guidance=guidance,
            evidence=[_annotate(hint, repo_path) for hint in guidance.evidence],
        ))

    report.advisories = _advisories(profile)
    return report


def _annotate(hint: str, repo_path: Optional[Path]) -> EvidenceHint:
    """Marks a hint present/absent when it names a path we can actually probe.

    Hints are written for humans, so plenty of them are prose ("the framework's
    own validation component"), alternatives ("pom.xml / build.gradle"), or a
    path plus a parenthetical ("package.json (dependencies)"). Strip a trailing
    parenthetical and probe only what's left if it's a single bare token —
    anything still containing whitespace stays unannotated, since a confident
    `found: false` about a sentence is worse than no answer at all.
    """
    if repo_path is None:
        return EvidenceHint(hint=hint)
    candidate = _PARENTHETICAL_RE.sub("", hint).strip()
    if not candidate or " " in candidate:
        return EvidenceHint(hint=hint)
    return EvidenceHint(hint=hint, found=(repo_path / candidate.rstrip("/")).exists())
