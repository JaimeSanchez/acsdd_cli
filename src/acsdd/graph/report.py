"""The human-readable half of every graph command.

Pure functions returning ``List[str]``; `cli.py` echoes them and adds colour.
`catalog.builder.build_catalog_markdown` set this precedent — a renderer that
returns text is testable without Click, and `cli.py` is already 1300 lines
before this subsystem adds seven printers to it.

Nothing here reads the filesystem or decides anything. If a renderer needs a
fact, it is passed the fact.
"""

import textwrap
from typing import Dict, List, Optional

from acsdd.graph import vocabulary
from acsdd.graph.changeset import ApplyOutcome
from acsdd.graph.integrity import IntegrityFinding, IntegrityReport
from acsdd.graph.model import Graph
from acsdd.graph.repository import GraphRevision

WIDTH = 78


def _wrap(text: str, indent: str = "    ") -> List[str]:
    return textwrap.fill(text, width=WIDTH, initial_indent=indent,
                         subsequent_indent=indent).splitlines()


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


# ---------------------------------------------------------------------
# integrity
# ---------------------------------------------------------------------

def render_findings(findings: List[IntegrityFinding], heading: str) -> List[str]:
    """One section of an integrity report.

    Findings of the same rule are grouped under one header rather than
    repeating the rule name per line — a graph with forty orphan nodes should
    read as one problem, not forty. The same grouping `cli._print_stale` does.
    """
    if not findings:
        return []

    lines = [heading, ""]
    by_rule: Dict[str, List[IntegrityFinding]] = {}
    for finding in findings:
        by_rule.setdefault(finding.rule, []).append(finding)

    for rule in sorted(by_rule):
        group = by_rule[rule]
        lines.append(f"  {rule}  ({_plural(len(group), 'finding')})")
        for finding in group:
            lines.append(f"    {finding.subject}")
            lines.extend(_wrap(finding.message, indent="      "))
            if finding.fix:
                lines.extend(_wrap(f"fix: {finding.fix}", indent="      "))
        lines.append("")

    return lines


def render_integrity_report(report: IntegrityReport, graph: Graph,
                            strict: bool = False) -> List[str]:
    lines = [
        f"Graph integrity — {_plural(len(graph.nodes), 'node')}, "
        f"{_plural(len(graph.edges), 'edge')}",
        "",
    ]
    lines.extend(render_findings(report.errors, "ERRORS"))
    lines.extend(render_findings(report.warnings, "WARNINGS"))
    lines.extend(render_findings(report.advisories, "ADVISORIES"))

    if report.errors:
        lines.append(f"{_plural(len(report.errors), 'error')} — "
                     f"`acsdd graph apply` will refuse this graph.")
    elif report.warnings and strict:
        lines.append(f"{_plural(len(report.warnings), 'warning')} — "
                     f"failing because --strict was given.")
    elif report.warnings:
        lines.append(f"No errors. {_plural(len(report.warnings), 'warning')} "
                     f"reported; they do not block `graph apply`.")
    else:
        lines.append("No errors, no warnings.")

    if report.advisories and not report.errors:
        lines.append(f"{_plural(len(report.advisories), 'advisory')} — "
                     f"informational, never blocking.")

    return lines


# ---------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------

def render_apply_outcome(outcome: ApplyOutcome, revision: Optional[GraphRevision],
                         dry_run: bool) -> List[str]:
    verb = "Would apply" if dry_run else "Applied"

    if outcome.is_noop:
        lines = ["Graph unchanged — every operation was already in effect."]
    else:
        lines = [f"{verb}:", ""]
        for label, ids in (("added node", outcome.added_nodes),
                           ("updated node", outcome.updated_nodes),
                           ("removed node", outcome.removed_nodes),
                           ("added edge", outcome.added_edges),
                           ("removed edge", outcome.removed_edges)):
            for node_id in ids:
                lines.append(f"  {label:<13} {node_id}")
        lines.append("")

    if outcome.no_ops:
        lines.append(f"  ({_plural(len(outcome.no_ops), 'operation')} already in effect)")

    if outcome.refusals:
        lines.append("")
        lines.append("REFUSED")
        for refusal in outcome.refusals:
            lines.extend(_wrap(refusal, indent="  "))

    if revision is not None and not outcome.is_noop:
        lines.append("")
        prefix = "Would create revision" if dry_run else "Revision"
        lines.append(f"{prefix} {revision.id} "
                     f"({revision.node_count} nodes, {revision.edge_count} edges)")

    return lines


# ---------------------------------------------------------------------
# show
# ---------------------------------------------------------------------

def render_graph_summary(graph: Graph) -> List[str]:
    """Counts by layer and type — the whole-graph view.

    Deliberately not a tree. `catalog build` already renders the capability
    dependency tree, and a second renderer of those same edges would drift from
    it; what this shows is the typed multi-edge shape, which is a different
    structure and a different question.
    """
    lines = [f"{_plural(len(graph.nodes), 'node')}, "
             f"{_plural(len(graph.edges), 'edge')}"]
    if graph.revision:
        lines.append(f"revision {graph.revision}")
    lines.append("")

    for layer in vocabulary.LAYERS:
        types = [t for t, spec in vocabulary.NODE_TYPES.items() if spec.layer == layer]
        counts = {t: len(graph.nodes_of_type(t)) for t in sorted(types)}
        total = sum(counts.values())
        if not total:
            continue
        lines.append(f"{layer} ({total})")
        for node_type, count in counts.items():
            if count:
                lines.append(f"  {count:>4}  {node_type}")
        lines.append("")

    edge_counts = {}
    for edge in graph.edges.values():
        edge_counts[edge.type] = edge_counts.get(edge.type, 0) + 1
    if edge_counts:
        lines.append("edges")
        for edge_type in sorted(edge_counts):
            lines.append(f"  {edge_counts[edge_type]:>4}  {edge_type}")

    if not graph.nodes:
        lines.append("(the graph is empty — nothing has been imported yet)")

    return lines


def render_node_neighbourhood(graph: Graph, node_id: str, depth: int) -> List[str]:
    """One node and what reaches it, out to `depth` hops.

    The typed-multi-edge render: each line names the edge type, so a reader
    sees *how* two nodes relate rather than only that they do.
    """
    node = graph.nodes.get(node_id)
    if node is None:
        return [f"No node '{node_id}' in the graph."]

    lines = [f"{node.id}  [{node.type} / {node.layer}]", f"  {node.name}"]
    if node.summary:
        lines.extend(_wrap(node.summary, indent="  "))
    if node.attributes:
        for key in sorted(node.attributes):
            lines.append(f"  {key}: {node.attributes[key]}")
    if node.confidence != 100:
        lines.append(f"  confidence: {node.confidence}")
    if node.status != "active":
        lines.append(f"  status: {node.status}")
    for evidence in node.evidence:
        location = f"{evidence.path}:{evidence.lines}" if evidence.lines else evidence.path
        lines.append(f"  evidence: {location}")
    lines.append("")

    seen = {node_id}
    frontier = [node_id]
    for hop in range(1, depth + 1):
        rows: List[str] = []
        next_frontier: List[str] = []
        for current in frontier:
            for edge in graph.out_edges(current):
                rows.append(f"  {current} --{edge.type}--> {edge.to_id}")
                if edge.to_id not in seen:
                    next_frontier.append(edge.to_id)
            for edge in graph.in_edges(current):
                rows.append(f"  {edge.from_id} --{edge.type}--> {current}")
                if edge.from_id not in seen:
                    next_frontier.append(edge.from_id)
        if not rows:
            break
        lines.append(f"hop {hop}")
        lines.extend(sorted(set(rows)))
        lines.append("")
        seen.update(next_frontier)
        frontier = sorted(set(next_frontier))
        if not frontier:
            break

    return lines


def render_node_list(nodes: List, heading: str) -> List[str]:
    if not nodes:
        return [f"{heading}: none"]
    lines = [heading, ""]
    for node in nodes:
        lines.append(f"  {node.id:<32} {node.type:<20} {node.name}")
    return lines


# ---------------------------------------------------------------------
# revisions
# ---------------------------------------------------------------------

def render_c4_classification(report: Dict) -> List[str]:
    """The impact table `graph diff --for c4` prints.

    Grouped by status because that is how the diagram is drawn, and in the
    order a reader cares about: what is new, what changed, what went, and only
    then what merely sits next to it.
    """
    counts = report["counts"]
    lines = [
        "Architectural impact — "
        + ", ".join(f"{counts[s]} {s}" for s in report["statuses"]),
        "",
    ]

    if not report["components"]:
        lines.append("(nothing diagrammable changed — this change touches no "
                     "components, containers, interfaces or data entities)")
        return lines

    for status in report["statuses"]:
        group = report["by_status"][status]
        if not group:
            continue
        lines.append(f"{status.upper()}")
        for component in group:
            technology = f"  [{component['technology']}]" if component["technology"] else ""
            lines.append(f"  {component['id']:<32} {component['type']:<14} "
                         f"{component['name']}{technology}")
            for evidence in component["evidence"]:
                location = (f"{evidence['path']}:{evidence['lines']}"
                            if evidence.get("lines") else evidence["path"])
                lines.append(f"      {location}")
        lines.append("")

    return lines


def render_revisions(revisions: List[GraphRevision]) -> List[str]:
    if not revisions:
        return ["No revisions yet — nothing has been applied to this graph."]

    lines = [f"{_plural(len(revisions), 'revision')}, newest last", ""]
    for revision in revisions:
        change = f"  ({revision.changeset_id})" if revision.changeset_id else ""
        lines.append(f"  {revision.id}  {revision.applied_at}  "
                     f"{revision.node_count:>4}n {revision.edge_count:>4}e{change}")
        if revision.title:
            lines.extend(_wrap(revision.title, indent="      "))
    return lines
