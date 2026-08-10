"""The `graph apply` transaction: schema, then integrity, then the write.

`plan_apply` works out everything that would happen — the resulting graph, the
findings, the revision that would be cut — **without touching the disk**. That
is `capability.remover.plan_removal`'s charter applied to a mutation, and it is
what makes ``--dry-run`` the real thing with the write left off rather than a
separate code path that can disagree with it.

`commit_apply` is the only function here that writes, and it refuses to write
anything the plan did not approve.

Three layers of idempotency, in the order they are cheapest to check:

1. an ``applied.json`` beside the changeset catches a literal re-run before a
   single operation is replayed;
2. derived edge ids and id-keyed nodes collapse repeats into
   `ApplyOutcome.no_ops`;
3. an outcome that changed nothing writes nothing and cuts no revision.

**`--force` means exactly one thing here: proceed past a stale
`base_revision`.** It never overrides an integrity error. That mirrors the rule
`capability remove` already follows — consent to rewrite your own graph is not
consent to leave it internally inconsistent — and it is why `graph apply` is
not `--force`-gated in general: refusing to apply a validated changeset without
a flag would make the subsystem unusable unattended.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from acsdd.graph.changeset import ApplyOutcome, GraphChangeSet, apply_operations
from acsdd.graph.integrity import (
    IntegrityFinding,
    IntegrityReport,
    RuleContext,
    check_integrity,
)
from acsdd.graph.model import Graph
from acsdd.graph.repository import GraphRevision, JsonGraphRepository
from acsdd.graph.validator import validate_changeset_document, validate_graph_document


@dataclass
class ApplyPlan:
    """What `graph apply` would do, and everything standing in its way."""

    changeset: GraphChangeSet
    outcome: ApplyOutcome
    integrity: IntegrityReport
    revision: Optional[GraphRevision]
    base_revision: Optional[str]
    current_revision: Optional[str]
    schema_errors: List[str] = field(default_factory=list)
    already_applied_as: Optional[str] = None

    @property
    def base_is_stale(self) -> bool:
        """The changeset was computed against a graph that has since moved.

        `None` means "any base" — a changeset written before the graph had a
        revision at all, which is the normal first-import case.
        """
        if self.base_revision is None:
            return False
        return self.base_revision != self.current_revision

    @property
    def blocked_reasons(self) -> List[str]:
        """Everything that stops this being written, in the order it is hit.

        Schema and integrity failures are absolute; a stale base is the one
        entry `--force` can clear, and `can_commit` is what decides that.
        """
        reasons = []
        if self.schema_errors:
            reasons.append(
                f"the changeset does not match the schema "
                f"({len(self.schema_errors)} error(s))")
        if self.integrity.errors:
            reasons.append(
                f"the resulting graph would have "
                f"{len(self.integrity.errors)} integrity error(s)")
        if self.base_is_stale:
            reasons.append(
                f"the changeset was computed against revision "
                f"{self.base_revision}, but the graph is at "
                f"{self.current_revision or 'no revision'}")
        return reasons

    def can_commit(self, force: bool = False) -> bool:
        if self.schema_errors or self.integrity.errors:
            return False  # --force never overrides these
        if self.base_is_stale and not force:
            return False
        return True

    def to_dict(self) -> Dict:
        return {
            "changeset_id": self.changeset.id,
            "title": self.changeset.title,
            "base_revision": self.base_revision,
            "current_revision": self.current_revision,
            "base_is_stale": self.base_is_stale,
            "already_applied_as": self.already_applied_as,
            "schema_errors": self.schema_errors,
            "blocked_reasons": self.blocked_reasons,
            "revision": self.revision.to_dict() if self.revision else None,
            **self.outcome.to_dict(),
            "integrity": self.integrity.to_dict(),
        }


def plan_apply(repo: JsonGraphRepository, changeset: GraphChangeSet,
               changeset_document: Optional[Dict] = None,
               ctx: Optional[RuleContext] = None,
               already_applied_as: Optional[str] = None,
               applied_at: Optional[date] = None) -> ApplyPlan:
    """Everything applying `changeset` would do. Writes nothing.

    `changeset_document` is the raw dict the changeset was parsed from; when
    given it is schema-checked. It is optional so a caller holding a
    programmatically built changeset — the enrichment modules in Epics 4 and 5
    — is not forced to serialize it just to validate it.
    """
    schema_errors: List[str] = []
    if changeset_document is not None:
        schema_errors = validate_changeset_document(changeset_document).errors

    current = repo.load()
    outcome = apply_operations(current, changeset)
    integrity = check_integrity(outcome.graph, ctx)

    revision = None
    if not outcome.is_noop:
        revision = repo.plan_revision(outcome.graph, changeset.id, changeset.title,
                                      applied_at=applied_at)

    return ApplyPlan(
        changeset=changeset,
        outcome=outcome,
        integrity=integrity,
        revision=revision,
        base_revision=changeset.base_revision,
        current_revision=current.revision,
        schema_errors=schema_errors,
        already_applied_as=already_applied_as,
    )


def commit_apply(repo: JsonGraphRepository, plan: ApplyPlan, force: bool = False,
                 acsdd_version: str = "",
                 generated_at: Optional[date] = None) -> Optional[GraphRevision]:
    """Write the planned graph. Returns the revision cut, or None for a no-op.

    Raises `ValueError` when the plan is not committable — callers are expected
    to have checked `can_commit` and rendered the reasons; reaching here with a
    blocked plan is a programming error, not a user one.
    """
    if not plan.can_commit(force=force):
        raise ValueError("; ".join(plan.blocked_reasons) or "plan is not committable")

    if plan.outcome.is_noop or plan.revision is None:
        return None  # nothing changed: write nothing, cut no revision

    repo.save(plan.outcome.graph, plan.revision, acsdd_version=acsdd_version,
              generated_at=generated_at)
    return plan.revision


def validate_graph(graph: Graph, ctx: Optional[RuleContext] = None) -> IntegrityReport:
    """`acsdd graph validate`'s integrity half. Here rather than in `cli.py` so
    it stays callable — and testable — without Click."""
    return check_integrity(graph, ctx)


def schema_findings(document: Optional[Dict]) -> List[IntegrityFinding]:
    """Schema errors for an on-disk graph document, as integrity findings.

    `graph apply` only ever writes documents this schema accepts, so on a graph
    acsdd built this always comes back empty. It exists for the ones it did not
    build: a hand-edited `graph.json`, a bad merge resolution, a file written by
    some future tool. Folding the result into the same report means
    `graph validate` reports one list of problems rather than making the reader
    check two places, and it is the only thing that loads
    ``engineering-graph.schema.json`` — a schema nothing reads is a schema that
    silently stops matching reality.
    """
    if document is None:
        return []

    return [
        IntegrityFinding(
            rule="schema", subject=error.split(":", 1)[0].strip() or "<root>",
            message=error,
            fix="fix it by hand, or re-apply the changesets under .acsdd/changes/")
        for error in validate_graph_document(document).errors
    ]
