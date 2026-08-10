"""acsdd — command-line tool for the ACSDD framework.

Subcommand groups:
  acsdd capability   validate / list / show / generate / recommend / remove
  acsdd catalog      build / verify
  acsdd profile      discover / validate / create / review / remove
  acsdd graph        show / validate / apply / context / revisions
  acsdd change       new / list / show / remove
  acsdd skill        list / install / show / remove

Top-level commands:
  acsdd update       self-update the standalone binary install
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import click

from acsdd import __version__
from acsdd.capability.generator import scaffold_manifest
from acsdd.capability.loader import iter_manifests, ManifestLoadError
from acsdd.capability.remover import CapabilityNotFoundError, plan_removal
from acsdd.capability.validator import validate_manifest, validate_catalog
from acsdd.catalog.builder import build_catalog_markdown, CATEGORY_ORDER, CATEGORY_DOC_DIR
from acsdd.graph.model import GraphLoadError
from acsdd.paths import (
    ACSDD_DIR,
    DEFAULT_PROFILES_DIR,
    MANIFESTS_SUBDIR,
    change_artifact_paths,
    profile_artifact_paths,
    resolve_capabilities_dir,
    resolve_changes_dir,
    resolve_graph_dir,
    resolve_profiles_dir,
)
from acsdd.profile.generator import find_unresolved_fields, finalize_profile
from acsdd.profile.validator import validate_profile_file
from acsdd.removal import remove_paths
from acsdd.skills import (
    SkillError,
    find_skill,
    install_skill,
    is_installed,
    read_skill,
    remove_skill,
    resolve_targets,
    skill_paths,
)


_warned_legacy: set = set()


def _warn_legacy_layout(path: Path):
    """One-line nudge when a path resolved to the pre-.acsdd layout.

    Goes to stderr and fires at most once per kind per process: `--json`
    consumers read stdout, and a per-command banner would be noise on every
    invocation in a repo that hasn't renamed yet.
    """
    kind = path.name
    if kind in _warned_legacy:
        return
    _warned_legacy.add(kind)

    try:
        shown = path.relative_to(Path.cwd())
    except ValueError:
        shown = path

    click.secho(
        f"NOTE: using legacy '{shown}' — acsdd now keeps everything under "
        # Plain `mv`, not `git mv`: the latter fails outright when the
        # directory holds nothing git is tracking yet, and git detects the
        # rename from content at commit time either way.
        f"'{ACSDD_DIR}/'. Move it with: "
        f"mkdir -p {ACSDD_DIR} && mv {shown} {ACSDD_DIR}/{kind}",
        fg="yellow",
        err=True,
    )


def _require_force_to_remove(paths: list, force: bool):
    """Shows what a `remove` command would delete, and stops unless --force.

    Deliberately a flag rather than a click.confirm prompt: every other
    destructive edge in this tool is gated the same way ("re-run with --force
    to overwrite"), and a prompt would make the remove commands the only ones
    that can't run unattended.
    """
    if force:
        return

    click.echo("Would remove:")
    for path in paths:
        click.echo(f"  - {path}")
    click.secho(
        "ERROR: refusing to delete without confirmation — re-run with --force to remove.",
        fg="red",
    )
    sys.exit(1)


def _default_capabilities_dir() -> Path:
    """Walk up from cwd looking for a capabilities tree, falling back to
    ./.acsdd/capabilities. Keeps the CLI usable from any subdirectory of a
    project that has adopted the ACSDD layout. See acsdd.paths."""
    capabilities_dir, is_legacy = resolve_capabilities_dir(Path.cwd())
    if is_legacy:
        _warn_legacy_layout(capabilities_dir)
    return capabilities_dir


def _profiles_dir() -> Optional[Path]:
    """The repo's profiles directory, or None if it has none yet."""
    profiles_dir, is_legacy = resolve_profiles_dir(Path.cwd())
    if not profiles_dir.is_dir():
        return None
    if is_legacy:
        _warn_legacy_layout(profiles_dir)
    return profiles_dir


def _default_profile_path() -> Optional[Path]:
    """Looks for a single profile YAML under ./.acsdd/profiles — the default
    output location `profile discover`/`profile create` already write to —
    so a repo that's been onboarded doesn't need --profile spelled out by
    hand. Prefers a finalized profile (<id>.yaml) over a draft
    (<id>-draft.yaml); returns None (rather than guessing) whenever there's
    more than one plausible candidate."""
    profiles_dir = _profiles_dir()
    if profiles_dir is None:
        return None

    candidates = sorted(profiles_dir.glob("*.yaml"))
    finalized = [c for c in candidates if not c.name.endswith("-draft.yaml")]
    if len(finalized) == 1:
        return finalized[0]
    if not finalized and len(candidates) == 1:
        return candidates[0]
    return None


def _default_draft_profile_path() -> Optional[Path]:
    """Same idea as _default_profile_path, but for `profile create --draft`
    specifically: it consumes a *draft*, so this only ever looks at
    <id>-draft.yaml files under ./.acsdd/profiles, ignoring any already-
    finalized profile that might also be sitting there."""
    profiles_dir = _profiles_dir()
    if profiles_dir is None:
        return None

    drafts = sorted(profiles_dir.glob("*-draft.yaml"))
    if len(drafts) == 1:
        return drafts[0]
    return None


def _default_review_profile_path() -> Optional[Path]:
    """Auto-detection for `profile review`, which prefers a *draft*.

    The preference is inverted relative to _default_profile_path deliberately:
    that helper prefers a finalized profile, and a finalized profile by
    definition has nothing left to review — in a repo holding both, it would
    always report "nothing to do" while the draft that actually needed work sat
    right next to it. Falls back to _default_profile_path so reviewing a
    finalized profile still works when that's all there is.
    """
    return _default_draft_profile_path() or _default_profile_path()


@dataclass
class OnboardingStatus:
    """Snapshot of how far a repo has gotten through the Quickstart
    sequence, derived entirely from on-disk artifacts — no state is ever
    persisted, so this is recomputed fresh on every bare `acsdd` run."""

    has_profile_draft: bool
    has_finalized_profile: bool
    has_manifests: bool
    has_catalog: bool

    @property
    def is_onboarded(self) -> bool:
        # Catalog build is ongoing maintenance, not a one-time gate, so
        # it's tracked/shown but doesn't factor into "onboarded" status.
        return self.has_finalized_profile and self.has_manifests


def _onboarding_status() -> OnboardingStatus:
    profiles_dir = _profiles_dir()
    profile_candidates = sorted(profiles_dir.glob("*.yaml")) if profiles_dir else []
    has_finalized_profile = any(not p.name.endswith("-draft.yaml") for p in profile_candidates)

    capabilities_dir = _default_capabilities_dir()
    manifests_dir = capabilities_dir / MANIFESTS_SUBDIR
    has_manifests = manifests_dir.is_dir() and any(manifests_dir.glob("*.yaml"))
    has_catalog = (capabilities_dir / "CATALOG.md").exists()

    return OnboardingStatus(
        has_profile_draft=bool(profile_candidates),
        has_finalized_profile=has_finalized_profile,
        has_manifests=has_manifests,
        has_catalog=has_catalog,
    )


_BANNER = r"""
 █████╗  ██████╗███████╗██████╗ ██████╗
██╔══██╗██╔════╝██╔════╝██╔══██╗██╔══██╗
███████║██║     ███████╗██║  ██║██║  ██║
██╔══██║██║     ╚════██║██║  ██║██║  ██║
██║  ██║╚██████╗███████║██████╔╝██████╔╝
╚═╝  ╚═╝ ╚═════╝╚══════╝╚═════╝ ╚═════╝
"""


def _print_welcome(status: OnboardingStatus):
    def _step(done: bool, text: str):
        mark = "[x]" if done else "[ ]"
        click.secho(f"  {mark} {text}", fg="green" if done else "yellow")

    click.secho(_BANNER, fg="cyan", bold=True)
    click.secho("  AI-Collaborative Software Development & Delivery", dim=True)
    click.echo()
    click.echo("This repo isn't fully onboarded yet. Recommended steps:")
    click.echo()
    _step(status.has_profile_draft, "1. acsdd profile discover .      — scan the repo, write a draft profile")
    click.echo("      -  acsdd profile validate       — check the draft against the schema")
    click.echo("      -  acsdd profile review         — what's still [REVIEW REQUIRED], and how to resolve it")
    _step(status.has_finalized_profile, "2. acsdd profile create           — finalize once REVIEW REQUIRED fields are resolved")
    _step(status.has_manifests, "3. acsdd capability generate --id ID --category CAT  — scaffold a capability manifest")
    _step(status.has_catalog, "4. acsdd capability validate && acsdd catalog build")
    click.echo()
    # Deliberately outside the checklist: the graph is per-change, not
    # per-repo, so it must not gate `is_onboarded`. But a repo that finishes
    # the four steps above would otherwise never hear that the per-change half
    # exists, since this whole banner stops printing the moment it's onboarded.
    click.secho("  Then, per change:  acsdd change new \"...\"  →  "
                "acsdd graph context  →  acsdd graph apply", dim=True)
    click.echo()
    click.echo("See README.md#quickstart for details, or `acsdd COMMAND --help`.")


@click.group(invoke_without_command=True)
@click.version_option(version=__version__)
@click.pass_context
def cli(ctx: click.Context):
    """ACSDD — AI-Collaborative Software Development & Delivery CLI."""
    # Scoped to the invocation, not the interpreter: a real run is one command
    # per process, but the test suite drives many through a single one.
    _warned_legacy.clear()

    if ctx.invoked_subcommand is not None:
        return
    status = _onboarding_status()
    if status.is_onboarded:
        click.echo(ctx.get_help())
    else:
        _print_welcome(status)


@cli.command("update")
@click.option("--version", "version_tag", default=None,
              help="Specific release tag to install, e.g. v0.2.0 (default: latest).")
@click.option("--repo", default=None,
              help="GitHub owner/repo to update from (default: JaimeSanchez/acsdd_cli).")
def update(version_tag: Optional[str], repo: Optional[str]):
    """Update the standalone acsdd binary in place to the latest release.

    Only works for the curl-installed binary — for a source install, use
    `pip install --upgrade acsdd` or `git pull` instead.
    """
    from acsdd.update import perform_update, UpdateError, DEFAULT_REPO

    click.echo(f"Current version: {__version__}")
    try:
        tag = perform_update(repo=repo or DEFAULT_REPO, version=version_tag)
    except UpdateError as e:
        click.secho(f"ERROR: {e}", fg="red")
        sys.exit(1)

    click.secho(f"Updated to {tag}.", fg="green")
    click.echo("Re-run `acsdd --version` to confirm.")


# ---------------------------------------------------------------------
# capability
# ---------------------------------------------------------------------

@cli.group()
def capability():
    """Work with individual capability manifests."""


def _load_all(manifests_dir: Path) -> tuple[Dict[str, Dict], list[str]]:
    """Returns ({capability_id: raw_dict}, [load_errors])."""
    manifests: Dict[str, Dict] = {}
    load_errors: list[str] = []
    try:
        for path, data in iter_manifests(manifests_dir):
            cap_id = data.get("capability", {}).get("id") or path.stem
            manifests[cap_id] = data
    except ManifestLoadError as e:
        load_errors.append(str(e))
    return manifests, load_errors


def _require_manifests_dir(manifests_dir: Path) -> None:
    """Exit 1 with a next step when a command that *reads* manifests has no
    tree to read.

    `capability generate` is the only command that creates a capabilities tree;
    every other one consumes it. Auto-detection falls back to
    ./.acsdd/capabilities whether or not that exists, so without this guard a
    repo that has only been through `profile discover` reaches the read as if
    the tree were merely empty — which for `catalog build` meant writing
    CATALOG.md into a directory that isn't there, and a raw FileNotFoundError
    traceback out of the CLI.

    An existing-but-empty `_manifests/` is a different thing and stays legal:
    `removal.py` deliberately keeps the directory alive when its last manifest
    goes, so "zero capabilities" has to remain a buildable state.
    """
    if manifests_dir.is_dir():
        return
    click.secho(f"No manifests directory found at {manifests_dir}", fg="red")
    click.echo("This repo has no capabilities yet. Run `acsdd capability recommend` "
               "to see which ones its profile implies, then `acsdd capability "
               "generate --id ID --category CAT` to create one.")
    sys.exit(1)


@capability.command("validate")
@click.argument("path", type=click.Path(exists=True, path_type=Path), required=False)
@click.option("--manifests-dir", type=click.Path(path_type=Path), default=None,
              help="Directory of manifests (default: auto-detected .acsdd/capabilities/_manifests).")
def capability_validate(path: Optional[Path], manifests_dir: Optional[Path]):
    """Validate one manifest (PATH) or every manifest in --manifests-dir
    against the Appendix A schema, plus cross-manifest dependency checks."""
    manifests_dir = manifests_dir or _default_capabilities_dir() / MANIFESTS_SUBDIR

    if path:
        from acsdd.capability.loader import load_manifest
        try:
            data = load_manifest(path)
        except ManifestLoadError as e:
            click.secho(f"FAIL  {path}: {e}", fg="red")
            sys.exit(1)
        result = validate_manifest(path, data)
        if result.ok:
            click.secho(f"PASS  {result.capability_id or path.name}", fg="green")
            return
        click.secho(f"FAIL  {result.capability_id or path.name}", fg="red")
        for err in result.errors:
            click.echo(f"  - {err}")
        sys.exit(1)

    _require_manifests_dir(manifests_dir)

    manifests, load_errors = _load_all(manifests_dir)
    any_fail = bool(load_errors)
    for err in load_errors:
        click.secho(f"FAIL  {err}", fg="red")

    for cap_id, data in manifests.items():
        cap_path = manifests_dir / f"{cap_id}.yaml"
        result = validate_manifest(cap_path, data)
        if result.ok:
            click.secho(f"PASS  {cap_id}", fg="green")
        else:
            any_fail = True
            click.secho(f"FAIL  {cap_id}", fg="red")
            for err in result.errors:
                click.echo(f"  - {err}")

    cross_problems = validate_catalog(manifests)
    for cap_id, errs in cross_problems.items():
        any_fail = True
        for err in errs:
            click.secho(f"FAIL  {cap_id}: {err}", fg="red")

    sys.exit(1 if any_fail else 0)


@capability.command("list")
@click.option("--manifests-dir", type=click.Path(path_type=Path), default=None)
@click.option("--category", default=None, help="Filter by category, e.g. DB.")
def capability_list(manifests_dir: Optional[Path], category: Optional[str]):
    """List every capability manifest found, sorted by ID."""
    manifests_dir = manifests_dir or _default_capabilities_dir() / MANIFESTS_SUBDIR
    manifests, load_errors = _load_all(manifests_dir)
    for err in load_errors:
        click.secho(f"WARN  {err}", fg="yellow")

    rows = []
    for cap_id, data in sorted(manifests.items()):
        cap = data.get("capability", {})
        if category and cap.get("category") != category:
            continue
        deps = ", ".join(d.get("capability", "?") for d in cap.get("dependencies", []) or []) or "—"
        rows.append((cap.get("id", cap_id), cap.get("category", "?"), cap.get("name", "?"),
                      cap.get("version", "?"), deps))

    if not rows:
        click.echo("No capabilities found.")
        return

    widths = [max(len(r[i]) for r in rows + [("ID", "CAT", "NAME", "VER", "DEPENDS ON")]) for i in range(5)]
    header = ("ID", "CAT", "NAME", "VER", "DEPENDS ON")
    click.echo("  ".join(h.ljust(w) for h, w in zip(header, widths)))
    click.echo("  ".join("-" * w for w in widths))
    for row in rows:
        click.echo("  ".join(str(c).ljust(w) for c, w in zip(row, widths)))


@capability.command("show")
@click.argument("capability_id")
@click.option("--manifests-dir", type=click.Path(path_type=Path), default=None)
def capability_show(capability_id: str, manifests_dir: Optional[Path]):
    """Print the full manifest for one capability ID, plus its resolved
    dependency chain."""
    manifests_dir = manifests_dir or _default_capabilities_dir() / MANIFESTS_SUBDIR
    manifests, _ = _load_all(manifests_dir)

    if capability_id not in manifests:
        click.secho(f"Capability '{capability_id}' not found in {manifests_dir}", fg="red")
        sys.exit(1)

    import yaml as _yaml
    click.echo(_yaml.dump(manifests[capability_id], default_flow_style=False, sort_keys=False))

    chain = []
    seen = set()
    frontier = [capability_id]
    while frontier:
        current = frontier.pop()
        cap = manifests.get(current, {}).get("capability", {})
        for dep in cap.get("dependencies", []) or []:
            dep_id = dep.get("capability")
            if dep_id and dep_id not in seen:
                seen.add(dep_id)
                chain.append(dep_id)
                frontier.append(dep_id)

    if chain:
        click.echo("Resolved dependency chain: " + " -> ".join(chain))


_CAP_ID_PATTERN = r"^[A-Z]{2,4}-[0-9]{3}$"


@capability.command("generate")
@click.option("--profile", "profile_path", type=click.Path(exists=True, path_type=Path), default=None,
              help="Path to a profile YAML (default: auto-detected single profile under ./.acsdd/profiles).")
@click.option("--id", "cap_id", required=True, help="Capability id, e.g. BE-005.")
@click.option("--category", required=True, type=click.Choice(CATEGORY_ORDER))
@click.option("--name", default=None, help="Human-readable name (optional; left as a placeholder if omitted).")
@click.option("--capabilities-dir", type=click.Path(path_type=Path), default=None,
              help="Root capabilities directory (default: auto-detected under .acsdd/, created if missing).")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite an existing manifest and/or procedure-doc stub.")
def capability_generate(profile_path: Optional[Path], cap_id: str, category: str, name: Optional[str],
                         capabilities_dir: Optional[Path], force: bool):
    """Scaffold a new draft capability manifest from a profile.

    Fills in everything derivable from the profile (adapter stack, profile
    constraints, quality gates); everything requiring actual knowledge of
    this specific capability is left as a [REVIEW REQUIRED] placeholder.
    """
    import re
    if not re.match(_CAP_ID_PATTERN, cap_id):
        click.secho(f"Invalid --id '{cap_id}' — expected a pattern like BE-005.", fg="red")
        sys.exit(1)

    if profile_path is None:
        profile_path = _default_profile_path()
        if profile_path is None:
            click.secho(
                "ERROR: no --profile given and couldn't auto-detect one under ./.acsdd/profiles.",
                fg="red",
            )
            click.echo("Run `acsdd profile discover .` first, or pass --profile explicitly.")
            sys.exit(1)
        click.echo(f"No --profile given, using: {profile_path}")

    import yaml as _yaml
    profile_doc = _yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    profile = profile_doc.get("profile", {}) or {}

    capabilities_dir = capabilities_dir or _default_capabilities_dir()
    manifests_dir = capabilities_dir / MANIFESTS_SUBDIR
    manifests_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = manifests_dir / f"{cap_id}.yaml"
    doc_dir = capabilities_dir / CATEGORY_DOC_DIR.get(category, category.lower())
    doc_path = doc_dir / f"{cap_id.lower()}.md"

    existing = [p for p in (manifest_path, doc_path) if p.exists()]
    if existing and not force:
        click.secho("ERROR: file(s) already exist — re-run with --force to overwrite:", fg="red")
        for p in existing:
            click.echo(f"   {p}")
        sys.exit(1)

    manifest = scaffold_manifest(profile, cap_id, category, name)
    manifest_path.write_text(
        _yaml.dump(manifest, default_flow_style=False, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    click.secho(f"Wrote {manifest_path}", fg="green")

    doc_dir.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(
        f"# {cap_id}\n\n"
        f"**Manifest:** `_manifests/{cap_id}.yaml`\n\n"
        "[REVIEW REQUIRED] — describe the procedure an AI agent follows to "
        "execute this capability.\n",
        encoding="utf-8",
    )
    click.secho(f"Wrote {doc_path}", fg="green")

    result = validate_manifest(manifest_path, manifest)
    if result.ok:
        click.secho("Schema: valid draft (still has [REVIEW REQUIRED] fields to fill in)", fg="yellow")
    else:
        click.secho("Schema: FAILED — this shouldn't happen for a freshly scaffolded draft:", fg="red")
        for err in result.errors:
            click.echo(f"  - {err}")

    click.echo()
    click.echo("Next steps:")
    click.echo(f"  1. Fill in name/description/inputs/outputs/adapter-id in {manifest_path}")
    click.echo(f"  2. Write the procedure doc body in {doc_path}")
    click.echo(f"  3. acsdd capability validate {manifest_path}")
    click.echo("  4. acsdd catalog build")


@capability.command("recommend")
@click.option("--profile", "profile_path", type=click.Path(exists=True, path_type=Path), default=None,
              help="Path to a profile YAML (default: auto-detected single profile under ./.acsdd/profiles).")
@click.option("--capabilities-dir", type=click.Path(path_type=Path), default=None,
              help="Root capabilities directory (default: auto-detected).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the report as JSON on stdout (what the capability-plan skill consumes).")
def capability_recommend(profile_path: Optional[Path], capabilities_dir: Optional[Path],
                          as_json: bool):
    """Suggest the capability set this profile implies, and flag drift.

    `capability generate` presumes you already know which capabilities your
    repo should have. This answers that: it maps the profile's traits (an ORM
    is configured, a frontend exists, a test framework was detected) onto the
    capabilities a repo with those traits wants, and marks each as already
    covered or still missing.

    Re-run it after any profile change. A profile is not a one-time artifact —
    when a library upgrade moves technology_stack, the manifests written
    against the old profile keep asserting constraints and quality gates that
    stopped being true, and the second half of this report is what surfaces
    them.

    Informational, not a gate: gaps and drift are its expected input, so it
    exits 0 on both. Exit 1 is reserved for real failures (no profile found,
    unparseable YAML).
    """
    import json as _json
    import yaml as _yaml

    from acsdd.capability.recommender import recommend

    if profile_path is None:
        profile_path = _default_profile_path()
        if profile_path is None:
            click.secho(
                "ERROR: no --profile given and couldn't auto-detect one under ./.acsdd/profiles.",
                fg="red",
            )
            click.echo("Run `acsdd profile discover .` first, or pass --profile explicitly.")
            sys.exit(1)
        # Chatter goes to stderr so --json keeps stdout to the payload alone.
        click.echo(f"No --profile given, using: {profile_path}", err=as_json)

    try:
        data = _yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    except _yaml.YAMLError as exc:
        click.secho(f"ERROR: {profile_path} is not parseable YAML:", fg="red")
        click.echo(f"  - {exc}")
        sys.exit(1)

    capabilities_dir = capabilities_dir or _default_capabilities_dir()
    manifests_dir = capabilities_dir / MANIFESTS_SUBDIR
    try:
        manifests = list(iter_manifests(manifests_dir)) if manifests_dir.is_dir() else []
    except ManifestLoadError as exc:
        click.secho(f"ERROR: {exc}", fg="red")
        sys.exit(1)

    report = recommend(data.get("profile", {}) or {}, manifests)

    if as_json:
        payload = {
            "acsdd_version": __version__,
            "profile_path": str(profile_path),
            "capabilities_dir": str(capabilities_dir),
            **report.to_dict(),
        }
        click.echo(_json.dumps(payload, indent=2, sort_keys=False))
        return

    _print_recommendations(report)
    _print_stale(report)

    click.echo("Next:")
    if report.missing:
        first = report.missing[0]
        click.echo(f"  1. {first.generate_command}   (and the rest above)")
        click.echo("  2. fill in name/description/inputs/outputs in each manifest")
        click.echo("  3. acsdd capability validate && acsdd catalog build")
    elif report.stale:
        click.echo("  1. update the stale fields above in each manifest")
        click.echo("  2. acsdd capability validate && acsdd catalog build")
    else:
        click.echo("  nothing — the capability set matches the profile.")

    # Contextual, not part of the onboarding checklist: only surface the skill
    # to someone who has work it could do and hasn't already installed it.
    if (report.missing or report.stale) and not is_installed("capability-plan", Path.cwd()):
        click.echo()
        click.secho(
            "Tip: `acsdd skill install` drops a Claude Code skill into this repo "
            "that can do steps 1-2 for you.",
            dim=True,
        )


def _print_recommendations(report) -> None:
    """Recommendations grouped by category, gaps first within each group."""
    import textwrap

    if not report.recommended:
        click.secho("No capabilities recommended — the profile carries too few "
                    "resolved fields to infer anything.", fg="yellow")
        click.echo("Resolve it with `acsdd profile review`, then re-run this.\n")
        return

    click.secho(
        f"{len(report.missing)} capability gap(s), "
        f"{len(report.recommended) - len(report.missing)} already covered.",
        bold=True,
    )
    click.echo()

    by_category: Dict[str, list] = {}
    for rec in report.recommended:
        by_category.setdefault(rec.category, []).append(rec)

    for category, entries in by_category.items():
        click.secho(f"{category}", fg="cyan", bold=True)
        for rec in sorted(entries, key=lambda r: r.status != "missing"):
            if rec.status == "covered":
                click.secho(f"  [x] {rec.name}", fg="green")
                click.echo(f"        covered by {', '.join(rec.covered_by)}")
                continue
            click.secho(f"  [ ] {rec.suggested_id}  {rec.name}", fg="yellow")
            click.echo(textwrap.fill(rec.why, width=78,
                                     initial_indent=" " * 8, subsequent_indent=" " * 8))
            if rec.depends_on:
                click.echo(f"        depends on: {', '.join(rec.depends_on)}")
            for note in rec.blocked_by:
                click.echo(textwrap.fill(f"blocked: {note}", width=78,
                                         initial_indent=" " * 8,
                                         subsequent_indent=" " * 17))
        click.echo()


def _print_stale(report) -> None:
    """Drift first, then advisories — collapsed, since an identical advisory
    across every manifest is one decision, not N."""
    import textwrap

    if report.stale:
        # One library upgrade lands the identical finding on every manifest
        # that pinned it. Printing it per manifest turns a three-line answer
        # into a wall; the JSON keeps the per-manifest granularity that a
        # consumer editing files actually needs.
        groups: Dict[tuple, list] = {}
        for entry in report.stale:
            groups.setdefault(
                (entry.field, entry.manifest_value, entry.profile_value, entry.fix),
                [],
            ).append(entry.id)

        affected = {entry.id for entry in report.stale}
        click.secho(
            f"{len(groups)} stale field(s) across {len(affected)} manifest(s) — "
            "the profile has moved past these:",
            fg="red", bold=True,
        )
        for (field_name, manifest_value, profile_value, fix), ids in groups.items():
            click.secho(f"  {field_name}  ({', '.join(ids)})", fg="yellow")
            click.echo(f"        manifest: {manifest_value}")
            click.echo(f"        profile:  {profile_value}")
            click.echo(textwrap.fill(fix, width=78,
                                     initial_indent=" " * 8 + "fix: ",
                                     subsequent_indent=" " * 13))
        click.echo()

    if report.advisories:
        grouped: Dict[str, list] = {}
        for advisory in report.advisories:
            grouped.setdefault(advisory.why, []).append(advisory.id)
        click.secho("Advisories (never blocking):", fg="yellow")
        for why, ids in grouped.items():
            click.echo(textwrap.fill(f"{', '.join(ids)} — {why}", width=78,
                                     initial_indent="  - ", subsequent_indent=" " * 4))
        click.echo()


@capability.command("remove")
@click.argument("capability_id")
@click.option("--capabilities-dir", type=click.Path(path_type=Path), default=None,
              help="Root capabilities directory (default: auto-detected).")
@click.option("--force", is_flag=True, default=False,
              help="Actually delete. Without it, the files are only listed.")
@click.option("--no-catalog", is_flag=True, default=False,
              help="Skip regenerating CATALOG.md afterwards.")
def capability_remove(capability_id: str, capabilities_dir: Optional[Path],
                       force: bool, no_catalog: bool):
    """Delete a capability's manifest and its procedure doc.

    Refuses outright if any other capability depends on this one — that would
    leave dangling references that `capability validate` and `catalog verify`
    both fail on. Rebuilds CATALOG.md afterwards so the catalog doesn't go
    stale behind you.
    """
    capabilities_dir = capabilities_dir or _default_capabilities_dir()
    manifests_dir = capabilities_dir / MANIFESTS_SUBDIR
    manifests, load_errors = _load_all(manifests_dir)
    for err in load_errors:
        click.secho(f"WARN  {err}", fg="yellow")

    try:
        plan = plan_removal(capabilities_dir, manifests_dir, manifests, capability_id)
    except CapabilityNotFoundError:
        click.secho(f"Capability '{capability_id}' not found in {manifests_dir}", fg="red")
        sys.exit(1)

    # Checked before the --force gate on purpose: --force is consent to delete
    # your own files, not consent to break every other manifest in the tree.
    if plan.dependents:
        click.secho(
            f"ERROR: cannot remove {capability_id} — "
            f"{len(plan.dependents)} capabilit{'y' if len(plan.dependents) == 1 else 'ies'} "
            f"depend{'s' if len(plan.dependents) == 1 else ''} on it:",
            fg="red",
        )
        for dep_id, dep_name in plan.dependents:
            click.echo(f"  - {dep_id} ({dep_name})")
        click.echo("Update or remove those first.")
        sys.exit(1)

    _require_force_to_remove(plan.paths, force)

    for path in remove_paths(plan.paths, protect=[manifests_dir]):
        click.secho(f"Removed {path}", fg="green")

    catalog_path = capabilities_dir / "CATALOG.md"
    if no_catalog or not catalog_path.exists():
        return

    remaining = {k: v for k, v in manifests.items() if k != capability_id}
    catalog_path.write_text(
        build_catalog_markdown(remaining, docs_root=capabilities_dir,
                               manifests_root=manifests_dir),
        encoding="utf-8",
    )
    click.secho(f"Wrote {catalog_path} ({len(remaining)} capabilities)", fg="green")


# ---------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------

@cli.group()
def catalog():
    """Build and verify the capability catalog (CATALOG.md)."""


@catalog.command("build")
@click.option("--capabilities-dir", type=click.Path(path_type=Path), default=None,
              help="Root capabilities directory (default: auto-detected).")
@click.option("--out", type=click.Path(path_type=Path), default=None,
              help="Output path (default: <capabilities-dir>/CATALOG.md).")
def catalog_build(capabilities_dir: Optional[Path], out: Optional[Path]):
    """Regenerate CATALOG.md from the manifests in _manifests/. This file
    is generated — don't hand-edit it, since a rebuild will overwrite it."""
    capabilities_dir = capabilities_dir or _default_capabilities_dir()
    manifests_dir = capabilities_dir / MANIFESTS_SUBDIR
    _require_manifests_dir(manifests_dir)
    out = out or capabilities_dir / "CATALOG.md"

    manifests, load_errors = _load_all(manifests_dir)
    for err in load_errors:
        click.secho(f"WARN  {err}", fg="yellow")

    md = build_catalog_markdown(manifests, docs_root=capabilities_dir, manifests_root=manifests_dir)
    # The default `out` sits inside a tree the guard above proved exists; this
    # is for an explicit --out pointing somewhere that doesn't yet.
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    click.secho(f"Wrote {out} ({len(manifests)} capabilities)", fg="green")


@catalog.command("verify")
@click.option("--capabilities-dir", type=click.Path(path_type=Path), default=None)
def catalog_verify(capabilities_dir: Optional[Path]):
    """Fail if CATALOG.md is stale relative to the manifests, or if any
    manifest fails schema/cross-manifest validation. Intended for CI."""
    capabilities_dir = capabilities_dir or _default_capabilities_dir()
    manifests_dir = capabilities_dir / MANIFESTS_SUBDIR
    catalog_path = capabilities_dir / "CATALOG.md"

    # Before the staleness check, not after: with no tree at all the missing
    # CATALOG.md below would tell the user to run `catalog build`, which is the
    # one command that cannot help them here.
    _require_manifests_dir(manifests_dir)

    manifests, load_errors = _load_all(manifests_dir)
    any_fail = bool(load_errors)
    for err in load_errors:
        click.secho(f"FAIL  {err}", fg="red")

    for cap_id, data in manifests.items():
        result = validate_manifest(manifests_dir / f"{cap_id}.yaml", data)
        if not result.ok:
            any_fail = True
            click.secho(f"FAIL  {cap_id} failed schema validation", fg="red")

    cross_problems = validate_catalog(manifests)
    for cap_id, errs in cross_problems.items():
        any_fail = True
        for err in errs:
            click.secho(f"FAIL  {cap_id}: {err}", fg="red")

    if not catalog_path.exists():
        click.secho(f"FAIL  {catalog_path} does not exist — run `acsdd catalog build`", fg="red")
        any_fail = True
    else:
        expected = build_catalog_markdown(manifests, docs_root=capabilities_dir, manifests_root=manifests_dir,
                                           generated_at=_extract_catalog_date(catalog_path))
        actual = catalog_path.read_text(encoding="utf-8")
        if expected.strip() != actual.strip():
            click.secho(f"FAIL  {catalog_path} is out of date — run `acsdd catalog build`", fg="red")
            any_fail = True

    if not any_fail:
        click.secho("OK  catalog matches manifests, no cross-manifest issues", fg="green")

    sys.exit(1 if any_fail else 0)


def _extract_catalog_date(catalog_path: Path):
    """Pull the '**Last generated:** YYYY-MM-DD' line out of an existing
    CATALOG.md so verify can do a content-only diff, ignoring the fact that
    rebuilding today would naturally bump the date."""
    import re
    from datetime import date
    text = catalog_path.read_text(encoding="utf-8")
    m = re.search(r"\*\*Last generated:\*\*\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        return date.fromisoformat(m.group(1))
    return date.today()


# ---------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------

@cli.group()
def profile():
    """Discover, review, and validate ACSDD Engineering Profiles."""


@profile.command("discover")
@click.argument("repo_path", type=click.Path(exists=True, path_type=Path))
@click.option("--profile-id", default=None,
              help="Profile identifier (default: the repo directory's name).")
@click.option("--depth", default="deep", type=click.Choice(["surface", "deep"]))
@click.option("--known-stack", default=None)
@click.option("--target-version", default="0.2.0")
@click.option("--output", default=DEFAULT_PROFILES_DIR)
@click.option("--force", is_flag=True, default=False,
              help="Overwrite existing draft/report/recommendations files")
def profile_discover(repo_path, profile_id, depth, known_stack, target_version, output, force):
    """Run PROFILE-001 discovery against REPO_PATH (wraps the existing
    profile-discovery implementation)."""
    from acsdd.profile._discovery_impl import main as discovery_main

    if not profile_id:
        profile_id = repo_path.resolve().name
        click.echo(f"No --profile-id given, using directory name: {profile_id}")

    argv = [str(repo_path), "--profile-id", profile_id, "--depth", depth,
            "--target-version", target_version, "--output", output]
    if known_stack:
        argv += ["--known-stack", known_stack]
    if force:
        argv += ["--force"]
    discovery_main(argv)


@profile.command("create")
@click.option("--draft", "draft_path", type=click.Path(exists=True, path_type=Path), default=None,
              help="Path to a reviewed draft profile YAML (default: auto-detected single "
                   "*-draft.yaml under ./.acsdd/profiles).")
@click.option("--output", type=click.Path(path_type=Path), default=None,
              help="Output path (default: alongside --draft, named <profile-id>.yaml).")
@click.option("--force", is_flag=True, default=False, help="Overwrite an existing output file.")
def profile_create(draft_path: Optional[Path], output: Optional[Path], force: bool):
    """Finalize a reviewed draft profile into an active one.

    Refuses to finalize while any [REVIEW REQUIRED] placeholder remains
    unresolved — that's the actual gate this command exists to provide,
    since `profile validate` only checks schema shape.
    """
    if draft_path is None:
        draft_path = _default_draft_profile_path()
        if draft_path is None:
            click.secho(
                "ERROR: no --draft given and couldn't auto-detect one under ./.acsdd/profiles.",
                fg="red",
            )
            click.echo("Run `acsdd profile discover .` first, or pass --draft explicitly.")
            sys.exit(1)
        click.echo(f"No --draft given, using: {draft_path}")

    result = validate_profile_file(draft_path)
    if not result.ok:
        click.secho(f"FAIL  {draft_path} does not pass schema validation:", fg="red")
        for err in result.errors:
            click.echo(f"  - {err}")
        sys.exit(1)

    import yaml as _yaml
    data = _yaml.safe_load(draft_path.read_text(encoding="utf-8")) or {}
    profile = data.get("profile", {}) or {}

    unresolved = find_unresolved_fields(profile)
    if unresolved:
        click.secho(f"ERROR: {draft_path} still has unresolved [REVIEW REQUIRED] fields:", fg="red")
        for path in unresolved:
            click.echo(f"  - {path}")
        click.echo("\nResolve these fields, then re-run `acsdd profile create`.")
        sys.exit(1)

    finalized = finalize_profile(profile)

    if output is None:
        profile_id = finalized.get("meta", {}).get("id") or draft_path.stem
        output = draft_path.parent / f"{profile_id}.yaml"

    if output.exists() and not force:
        click.secho(f"ERROR: {output} already exists — re-run with --force to overwrite.", fg="red")
        sys.exit(1)

    output.write_text(
        _yaml.dump({"profile": finalized}, default_flow_style=False, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    click.secho(f"Wrote {output}", fg="green")
    # Points at `recommend` rather than straight at `generate`: knowing *which*
    # capabilities this repo wants is the question a freshly finalized profile
    # can now answer, and generate can't be run without that answer.
    click.echo(f"Next step: acsdd capability recommend --profile {output}")


@profile.command("validate")
@click.argument("profile_path", type=click.Path(exists=True, path_type=Path), required=False, default=None)
@click.option("--strict", is_flag=True, default=False,
              help="Also fail if any [REVIEW REQUIRED] placeholder remains unresolved "
                   "(schema validation alone can't tell a finished profile from a draft).")
def profile_validate(profile_path: Optional[Path], strict: bool):
    """Validate a Profile YAML file against the Appendix B schema.

    PROFILE_PATH is optional — omit it and acsdd auto-detects a single
    profile under ./.acsdd/profiles, same as `capability generate`.
    """
    if profile_path is None:
        profile_path = _default_profile_path()
        if profile_path is None:
            click.secho(
                "ERROR: no PROFILE_PATH given and couldn't auto-detect one under ./.acsdd/profiles.",
                fg="red",
            )
            click.echo("Run `acsdd profile discover .` first, or pass PROFILE_PATH explicitly.")
            sys.exit(1)
        click.echo(f"No PROFILE_PATH given, using: {profile_path}")

    result = validate_profile_file(profile_path)
    if not result.ok:
        click.secho(f"FAIL  {profile_path}", fg="red")
        for err in result.errors:
            click.echo(f"  - {err}")
        sys.exit(1)

    if strict:
        import yaml as _yaml
        data = _yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
        profile = data.get("profile", {}) or {}
        unresolved = find_unresolved_fields(profile)
        if unresolved:
            click.secho(
                f"FAIL  {profile_path} has {len(unresolved)} unresolved [REVIEW REQUIRED] field(s):",
                fg="red",
            )
            for path in unresolved:
                click.echo(f"  - {path}")
            sys.exit(1)

    click.secho(f"PASS  {profile_path}", fg="green")


@profile.command("review")
@click.argument("profile_path", type=click.Path(exists=True, path_type=Path), required=False, default=None)
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the report as JSON on stdout (what the profile-review skill consumes).")
@click.option("--repo-path", type=click.Path(exists=True, path_type=Path), default=Path("."),
              help="Repo root used to check whether each suggested evidence file exists.")
def profile_review(profile_path: Optional[Path], as_json: bool, repo_path: Path):
    """Explain what's still [REVIEW REQUIRED] in a profile, and how to resolve it.

    `profile validate --strict` tells you which fields are unresolved; this
    tells you what each one means, what discovery already tried, which files to
    look at, and what a plausible value looks like.

    Informational, not a gate: unresolved fields are this command's expected
    input, so it exits 0 even when it finds them. Exit 1 is reserved for real
    failures (no profile found, unparseable YAML). Wire `acsdd profile validate
    --strict` into CI — never this.
    """
    import json as _json
    import yaml as _yaml

    from acsdd.profile.review import review_profile

    if profile_path is None:
        profile_path = _default_review_profile_path()
        if profile_path is None:
            click.secho(
                "ERROR: no PROFILE_PATH given and couldn't auto-detect one under ./.acsdd/profiles.",
                fg="red",
            )
            click.echo("Run `acsdd profile discover .` first, or pass PROFILE_PATH explicitly.")
            sys.exit(1)
        # Chatter goes to stderr so --json keeps stdout to the payload alone.
        click.echo(f"No PROFILE_PATH given, using: {profile_path}", err=as_json)

    try:
        data = _yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    except _yaml.YAMLError as exc:
        click.secho(f"ERROR: {profile_path} is not parseable YAML:", fg="red")
        click.echo(f"  - {exc}")
        sys.exit(1)

    # A draft that's both structurally broken and incomplete is exactly the one
    # that most needs hints, so schema problems are reported and stepped over
    # rather than being a gate.
    result = validate_profile_file(profile_path)
    schema_errors = [] if result.ok else list(result.errors)

    report = review_profile(data.get("profile", {}) or {}, repo_path=repo_path)

    if as_json:
        payload = {
            "acsdd_version": __version__,
            "profile_path": str(profile_path),
            "schema_errors": schema_errors,
            **report.to_dict(),
        }
        click.echo(_json.dumps(payload, indent=2, sort_keys=False))
        return

    if schema_errors:
        click.secho(f"WARN  {profile_path} does not pass schema validation:", fg="yellow")
        for err in schema_errors:
            click.echo(f"  - {err}")
        click.echo()

    label = f" (profile: {report.profile_id})" if report.profile_id else ""
    click.echo(f"Reviewing {profile_path}{label}")
    click.echo()

    if not report.unresolved:
        click.secho(f"PASS  {profile_path} has no unresolved [REVIEW REQUIRED] fields", fg="green")
    else:
        click.echo(f"{len(report.unresolved)} unresolved [REVIEW REQUIRED] field(s):")
        click.echo()
        for entry in report.unresolved:
            _print_unresolved_field(entry)

    if report.advisories:
        import textwrap
        click.echo("Also worth setting (no placeholder, still at the discovery default):")
        for advisory in report.advisories:
            click.echo(textwrap.fill(
                f"{advisory.path} ({advisory.current_value!r}) — {advisory.why}",
                width=78, initial_indent="  - ", subsequent_indent=" " * 4,
            ))
        click.echo()

    click.echo("Next:")
    if report.unresolved:
        click.echo(f"  1. resolve the fields above in {profile_path}")
        click.echo(f"  2. acsdd profile validate {profile_path} --strict")
        click.echo("  3. acsdd profile create")
    else:
        click.echo("  acsdd profile create")

    # Contextual, not part of the onboarding checklist: only surface the skill
    # to someone who has work it could do and hasn't already installed it.
    if report.unresolved and not is_installed("profile-review", Path.cwd()):
        click.echo()
        click.secho(
            "Tip: `acsdd skill install` drops a Claude Code skill into this repo "
            "that can do steps 1-3 for you.",
            dim=True,
        )


def _print_unresolved_field(entry) -> None:
    """One block per unresolved field, wrapped so long hint text stays readable."""
    import textwrap

    def _wrap(label: str, text: str):
        body = textwrap.fill(text, width=78,
                             initial_indent=" " * 6 + f"{label:<13}",
                             subsequent_indent=" " * 19)
        click.echo(body)

    click.secho(f"  {entry.path}", fg="yellow")
    _wrap("placeholder:", entry.placeholder)
    _wrap("what:", entry.guidance.what)
    _wrap("tried:", entry.guidance.detection_attempted)

    if entry.evidence:
        rendered = [
            hint.hint if hint.found is None
            else f"{hint.hint} ({'found' if hint.found else 'missing'})"
            for hint in entry.evidence
        ]
        _wrap("look at:", ", ".join(rendered))
    if entry.guidance.allowed_values:
        _wrap("allowed:", ", ".join(entry.guidance.allowed_values))
    elif entry.guidance.examples:
        _wrap("examples:", ", ".join(entry.guidance.examples))

    for link in entry.resolved_links():
        _wrap("also set:", f"{link.path} — {link.note}")

    if entry.guidance.resolution == "rerun-discovery":
        _wrap("resolve:", f"re-run discovery — {entry.guidance.action}")
    else:
        _wrap("resolve:", "edit the draft in place")
    click.echo()


@profile.command("remove")
@click.argument("profile_id")
@click.option("--profiles-dir", type=click.Path(path_type=Path), default=None,
              help="Directory holding the profile (default: auto-detected .acsdd/profiles).")
@click.option("--force", is_flag=True, default=False,
              help="Actually delete. Without it, the files are only listed.")
def profile_remove(profile_id: str, profiles_dir: Optional[Path], force: bool):
    """Delete every artifact belonging to PROFILE_ID.

    That's the draft, the finalized profile, and the two discovery markdown
    reports — whichever of them exist. PROFILE_ID is required rather than
    auto-detected: the other profile commands guess a path only because
    guessing wrong there costs an error message.
    """
    if profiles_dir is None:
        profiles_dir = _profiles_dir()
        if profiles_dir is None:
            click.secho(
                "ERROR: no --profiles-dir given and this repo has no ./.acsdd/profiles.",
                fg="red",
            )
            sys.exit(1)

    existing = [p for p in profile_artifact_paths(profiles_dir, profile_id) if p.exists()]
    if not existing:
        click.secho(f"No artifacts for profile '{profile_id}' in {profiles_dir}", fg="red")
        sys.exit(1)

    _require_force_to_remove(existing, force)

    for path in remove_paths(existing, protect=[profiles_dir]):
        click.secho(f"Removed {path}", fg="green")


# ---------------------------------------------------------------------
# skill
# ---------------------------------------------------------------------

_AGENT_HELP = ("Agent convention to install for: 'agents' (.agents/skills/, read by "
               "Codex, Cursor, Kimi CLI, Gemini CLI, Copilot and others), 'claude' "
               "(.claude/skills/), or 'all'. Repeatable. Default: all.")


@cli.group()
def skill():
    """Install the agent skills acsdd ships into this repository.

    A noun group like capability/catalog/profile rather than a subcommand of
    any one of them — the assets aren't profile-specific, and future skills for
    capability authoring or catalog upkeep belong here too.
    """


@skill.command("list")
@click.option("--dir", "repo_root", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=Path("."), help="Repo root to check for existing installs (default: cwd).")
def skill_list(repo_root: Path):
    """List the skills acsdd ships, and whether each is installed here."""
    for asset in find_skill(None):
        installed = is_installed(asset.name, repo_root)
        mark = "[x]" if installed else "[ ]"
        click.secho(f"  {mark} {asset.name}", fg="green" if installed else "yellow")
        click.echo(f"        {asset.summary}")
        # Per-agent, because "installed" is not one state: a repo can be set up
        # for Claude Code and invisible to Codex, which is exactly the failure
        # worth surfacing here.
        for target, path in skill_paths(asset.name, repo_root):
            sub = "[x]" if path.exists() else "[ ]"
            click.echo(f"        {sub} {asset.dest(target)}   ({target.reads})")


@skill.command("install")
@click.argument("name", required=False, default=None)
@click.option("--dir", "repo_root", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=Path("."), help="Repo root to install into (default: cwd).")
@click.option("--agent", "agents", multiple=True, help=_AGENT_HELP)
@click.option("--force", is_flag=True, default=False, help="Overwrite an existing skill file.")
def skill_install(name: Optional[str], repo_root: Path, agents: tuple, force: bool):
    """Install a shipped skill for every agent convention (default: all skills).

    Writes one copy per agent target — `.agents/skills/` for the cross-agent
    convention and `.claude/skills/` for Claude Code — so the skill is visible
    whichever agent the repo is driven with.
    """
    try:
        assets = find_skill(name)
        resolve_targets(agents or None)
    except SkillError as exc:
        click.secho(f"ERROR: {exc}", fg="red")
        sys.exit(1)

    skipped = False
    for asset in assets:
        for result in install_skill(asset.name, repo_root, force=force,
                                    targets=agents or None):
            if result.written:
                click.secho(f"Wrote {result.path}", fg="green")
            else:
                skipped = True
                click.secho(
                    f"ERROR: {result.path} already exists — re-run with --force to overwrite.",
                    fg="red",
                )

    if skipped:
        sys.exit(1)


@skill.command("remove")
@click.argument("name")
@click.option("--dir", "repo_root", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=Path("."), help="Repo root to remove from (default: cwd).")
@click.option("--agent", "agents", multiple=True, help=_AGENT_HELP)
@click.option("--force", is_flag=True, default=False,
              help="Actually delete. Without it, the files are only listed.")
def skill_remove(name: str, repo_root: Path, agents: tuple, force: bool):
    """Remove an installed skill from every agent convention.

    NAME is required, unlike `skill install` where omitting it means "all of
    them" — that isn't a default worth having on a delete. The `.agents/skills/`
    and `.claude/skills/` directories themselves are left alone; they belong to
    other tools, not to acsdd.
    """
    try:
        find_skill(name)
        existing = [path for _, path in skill_paths(name, repo_root, agents or None)
                    if path.exists()]
    except SkillError as exc:
        click.secho(f"ERROR: {exc}", fg="red")
        sys.exit(1)

    if not existing:
        click.secho(f"ERROR: {name} is not installed.", fg="red")
        sys.exit(1)

    _require_force_to_remove(existing, force)

    for result in remove_skill(name, repo_root, targets=agents or None):
        if result.removed:
            click.secho(f"Removed {result.path}", fg="green")


@skill.command("show")
@click.argument("name")
def skill_show(name: str):
    """Print a shipped skill's markdown to stdout."""
    try:
        click.echo(read_skill(name))
    except SkillError as exc:
        click.secho(f"ERROR: {exc}", fg="red")
        sys.exit(1)


# ---------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------

def _graph_repo(graph_dir: Optional[Path]):
    from acsdd.graph.repository import JsonGraphRepository

    return JsonGraphRepository(graph_dir or resolve_graph_dir(Path.cwd()))


def _change_store(changes_dir: Optional[Path]):
    from acsdd.graph.repository import ChangeStore

    return ChangeStore(changes_dir or resolve_changes_dir(Path.cwd()))


def _default_change_id(store) -> Optional[str]:
    """The single *open* change, or None.

    Open means it has a change.json and no applied.json. Deliberately returns
    None rather than guessing whenever zero or more than one candidate exists —
    the rule `_default_profile_path` follows, and for a sharper reason: a wrong
    guess here silently writes a changeset into somebody else's change.
    """
    open_ids = store.open_ids()
    return open_ids[0] if len(open_ids) == 1 else None


def _resolve_change_id(store, change_id: Optional[str], as_json: bool = False,
                       required: bool = True) -> Optional[str]:
    if change_id:
        return change_id
    resolved = _default_change_id(store)
    if resolved is not None:
        click.echo(f"No --change given, using: {resolved}", err=as_json)
        return resolved
    if not required:
        return None

    candidates = store.open_ids()
    if not candidates:
        click.secho("ERROR: no --change given and this repo has no open changes.",
                    fg="red")
        click.echo("Start one with `acsdd change new \"Some title\"`.")
    else:
        click.secho("ERROR: no --change given and more than one change is open:",
                    fg="red")
        for candidate in candidates:
            click.echo(f"  - {candidate}")
        click.echo("Pass --change to say which.")
    sys.exit(1)


def _rule_context(change_id: Optional[str], capabilities_dir: Optional[Path],
                  changeset=None):
    """Build the RuleContext, reading manifests when a tree exists.

    Absent manifests mean the catalog rules stay silent rather than reporting
    every capability as uncatalogued — a report that cries wolf is one nobody
    reads.

    `changeset` supplies the node ids this change owns, which is what stops the
    scoping rule holding a new change responsible for an older one's names.
    """
    from acsdd.graph.integrity import RuleContext

    capabilities_dir = capabilities_dir or _default_capabilities_dir()
    manifests_dir = capabilities_dir / MANIFESTS_SUBDIR
    manifests: Dict[str, Dict] = {}
    if manifests_dir.is_dir():
        manifests, load_errors = _load_all(manifests_dir)
        for err in load_errors:
            click.secho(f"WARNING: {err}", fg="yellow", err=True)

    owned = None
    if changeset is not None:
        owned = frozenset(op.node.id for op in changeset.operations
                          if op.node is not None)

    return RuleContext(manifests=manifests, change_id=change_id, owned_node_ids=owned)


def _load_change_overlay(store, change_id: Optional[str]):
    """The graph a change's own changeset would produce, for validation.

    A change's business-layer nodes only exist inside its changeset until it is
    applied, so validating "the graph plus this change" means replaying the
    changeset rather than reading a second graph file.
    """
    from acsdd.graph.changeset import ChangeSetError

    if change_id is None or not store.changeset_path(change_id).is_file():
        return None
    try:
        return store.load_changeset(change_id)
    except (ChangeSetError, GraphLoadError) as exc:
        click.secho(f"ERROR: {store.changeset_path(change_id)}: {exc}", fg="red")
        sys.exit(1)


@cli.group()
def graph():
    """Work with the engineering knowledge graph.

    The graph is the canonical model of what this repository is and what a
    change does to it: requirements, the capabilities that deliver them, the
    components that implement those, and the modules and tests underneath. The
    refined spec, the C4 diagram and the implementation plan are projections of
    it rather than parallel sources of truth.

    acsdd does not interpret a PRD itself — that is judgement work, and it
    belongs to an agent following the packaged `graph-import` skill. What acsdd
    owns is the vocabulary the agent writes against (`graph context`) and the
    gate its output has to pass (`graph apply`).
    """


@graph.command("show")
@click.option("--node", "node_id", default=None,
              help="Show one node and its neighbourhood instead of a summary.")
@click.option("--type", "node_type", default=None,
              help="List only nodes of this type.")
@click.option("--layer", type=click.Choice(["business", "engineering", "technical"]),
              default=None, help="List only nodes in this layer.")
@click.option("--depth", type=int, default=1, show_default=True,
              help="How many hops to follow out from --node.")
@click.option("--graph-dir", type=click.Path(path_type=Path), default=None,
              help="Graph directory (default: auto-detected .acsdd/graph).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the selection as JSON on stdout.")
def graph_show(node_id: Optional[str], node_type: Optional[str], layer: Optional[str],
               depth: int, graph_dir: Optional[Path], as_json: bool):
    """Summarize the graph, or inspect one node's neighbourhood.

    Informational: always exits 0, including when the graph is empty.
    """
    import json as _json

    from acsdd.graph import report as graph_report
    from acsdd.graph import vocabulary

    repo = _graph_repo(graph_dir)
    try:
        current = repo.load()
    except GraphLoadError as exc:
        click.secho(f"ERROR: {exc}", fg="red")
        sys.exit(1)

    selected = None
    if node_type or layer:
        selected = [n for n in sorted(current.nodes.values(), key=lambda n: n.id)
                    if (node_type is None or n.type == node_type)
                    and (layer is None or vocabulary.NODE_TYPES.get(n.type)
                         and vocabulary.layer_of(n.type) == layer)]

    if as_json:
        if node_id:
            node = current.nodes.get(node_id)
            payload = {
                "node": node.to_dict() if node else None,
                "edges": [e.to_dict() for e in
                          current.out_edges(node_id) + current.in_edges(node_id)],
            }
        elif selected is not None:
            payload = {"nodes": [n.to_dict() for n in selected]}
        else:
            payload = {
                "nodes": [current.nodes[i].to_dict() for i in sorted(current.nodes)],
                "edges": [current.edges[i].to_dict() for i in sorted(current.edges)],
            }
        click.echo(_json.dumps({
            "acsdd_version": __version__,
            "graph_path": str(repo.graph_path),
            "revision": current.revision,
            **payload,
        }, indent=2, sort_keys=False))
        return

    if node_id:
        lines = graph_report.render_node_neighbourhood(current, node_id, max(depth, 0))
    elif selected is not None:
        heading = " ".join(filter(None, [layer, node_type, "nodes"]))
        lines = graph_report.render_node_list(selected, heading)
    else:
        lines = graph_report.render_graph_summary(current)

    for line in lines:
        click.echo(line)


@graph.command("validate")
@click.option("--change", "change_id", default=None,
              help="Also replay this change's changeset and validate the result.")
@click.option("--strict", is_flag=True, default=False,
              help="Exit 1 on warnings too, not only errors.")
@click.option("--graph-dir", type=click.Path(path_type=Path), default=None,
              help="Graph directory (default: auto-detected .acsdd/graph).")
@click.option("--changes-dir", type=click.Path(path_type=Path), default=None,
              help="Changes directory (default: auto-detected .acsdd/changes).")
@click.option("--capabilities-dir", type=click.Path(path_type=Path), default=None,
              help="Root capabilities directory (default: auto-detected).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the report as JSON on stdout.")
def graph_validate(change_id: Optional[str], strict: bool, graph_dir: Optional[Path],
                   changes_dir: Optional[Path], capabilities_dir: Optional[Path],
                   as_json: bool):
    """Check the graph against every integrity rule.

    Errors are what `graph apply` refuses to write: dangling edges, edge pairs
    the vocabulary forbids, cycles where the edge type forbids them, a
    Requirement nothing delivers. Warnings are reported and do not block —
    --strict is how you make them block. Advisories never affect the exit code.

    With --change, the change's changeset is replayed onto the graph first, so
    what gets validated is the graph as that change would leave it.
    """
    import json as _json

    from acsdd.graph import report as graph_report
    from acsdd.graph.applier import schema_findings, validate_graph
    from acsdd.graph.changeset import apply_operations

    repo = _graph_repo(graph_dir)
    store = _change_store(changes_dir)

    if change_id is None:
        change_id = _resolve_change_id(store, None, as_json=as_json, required=False)

    try:
        # The raw document is kept so the schema gets a look at it too: acsdd
        # only ever writes documents the schema accepts, but it is not the only
        # thing that can edit graph.json.
        document = repo.load_document() if repo.exists() else None
        current = repo.load()
    except GraphLoadError as exc:
        click.secho(f"ERROR: {exc}", fg="red")
        sys.exit(1)

    changeset = _load_change_overlay(store, change_id)
    if changeset is not None:
        current = apply_operations(current, changeset).graph

    report = validate_graph(
        current, _rule_context(change_id, capabilities_dir, changeset=changeset))
    report.errors[:0] = schema_findings(document)

    if as_json:
        click.echo(_json.dumps({
            "acsdd_version": __version__,
            "graph_path": str(repo.graph_path),
            "change_id": change_id,
            "strict": strict,
            "node_count": len(current.nodes),
            "edge_count": len(current.edges),
            **report.to_dict(),
        }, indent=2, sort_keys=False))
    else:
        for line in graph_report.render_integrity_report(report, current, strict=strict):
            click.echo(line)

    if report.errors or (strict and report.warnings):
        sys.exit(1)


@graph.command("apply")
@click.argument("changeset_path", type=click.Path(exists=True, path_type=Path),
                required=False)
@click.option("--change", "change_id", default=None,
              help="Apply this change's changeset.json instead of naming a path.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Report what would change and write nothing.")
@click.option("--force", is_flag=True, default=False,
              help="Proceed even when the changeset's base_revision is stale. "
                   "Never overrides an integrity error.")
@click.option("--graph-dir", type=click.Path(path_type=Path), default=None,
              help="Graph directory (default: auto-detected .acsdd/graph).")
@click.option("--changes-dir", type=click.Path(path_type=Path), default=None,
              help="Changes directory (default: auto-detected .acsdd/changes).")
@click.option("--capabilities-dir", type=click.Path(path_type=Path), default=None,
              help="Root capabilities directory (default: auto-detected).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the outcome as JSON on stdout.")
def graph_apply(changeset_path: Optional[Path], change_id: Optional[str], dry_run: bool,
                force: bool, graph_dir: Optional[Path], changes_dir: Optional[Path],
                capabilities_dir: Optional[Path], as_json: bool):
    """Validate a changeset and write it into the graph.

    Three gates, in order: the changeset must match its schema, the resulting
    graph must have no integrity errors, and the changeset's base_revision must
    match the graph's. Only the last is overridable, with --force.

    Applying the same changeset twice writes nothing and cuts no revision, so
    re-running after a partial failure is safe.

    Unlike the `remove` commands this is not --force-gated in general: refusing
    to write a validated changeset without a flag would make the whole
    subsystem unusable unattended.
    """
    import json as _json

    from acsdd.graph import report as graph_report
    from acsdd.graph.applier import commit_apply, plan_apply
    from acsdd.graph.changeset import ChangeSetError
    from acsdd.graph.repository import load_changeset, load_changeset_document

    repo = _graph_repo(graph_dir)
    store = _change_store(changes_dir)

    if changeset_path is None:
        change_id = _resolve_change_id(store, change_id, as_json=as_json)
        changeset_path = store.changeset_path(change_id)
        if not changeset_path.is_file():
            click.secho(f"ERROR: change '{change_id}' has no changeset at "
                        f"{changeset_path}.", fg="red")
            click.echo("An agent writes that file — see the `graph-import` skill, "
                       "or run `acsdd graph context --json` to get its contract.")
            sys.exit(1)

    try:
        document = load_changeset_document(changeset_path)
        changeset = load_changeset(changeset_path)
    except (ChangeSetError, GraphLoadError) as exc:
        click.secho(f"ERROR: {changeset_path}: {exc}", fg="red")
        sys.exit(1)

    # An applied.json is the cheapest of the three idempotency layers: it
    # catches a literal re-run before a single operation is replayed.
    already = store.applied_revision(changeset.id) if store.exists(changeset.id) else None

    try:
        plan = plan_apply(repo, changeset, changeset_document=document,
                          ctx=_rule_context(changeset.id, capabilities_dir,
                                            changeset=changeset),
                          already_applied_as=already)
    except GraphLoadError as exc:
        click.secho(f"ERROR: {exc}", fg="red")
        sys.exit(1)

    committed = None
    if not dry_run and plan.can_commit(force=force):
        committed = commit_apply(repo, plan, force=force, acsdd_version=__version__)
        if committed is not None and store.exists(changeset.id):
            store.mark_applied(changeset.id, committed.id)

    if as_json:
        click.echo(_json.dumps({
            "acsdd_version": __version__,
            "graph_path": str(repo.graph_path),
            "changeset_path": str(changeset_path),
            "dry_run": dry_run,
            "committed": committed.id if committed else None,
            **plan.to_dict(),
        }, indent=2, sort_keys=False))
    else:
        if already:
            click.secho(f"NOTE: change '{changeset.id}' was already applied as "
                        f"{already}.", fg="yellow")

        for line in graph_report.render_apply_outcome(
                plan.outcome, plan.revision, dry_run or not plan.can_commit(force=force)):
            click.echo(line)

        if plan.schema_errors:
            click.echo()
            click.secho("SCHEMA ERRORS", fg="red")
            for err in plan.schema_errors:
                click.echo(f"  - {err}")

        if plan.integrity.errors or plan.integrity.warnings or plan.integrity.advisories:
            click.echo()
            for line in graph_report.render_integrity_report(plan.integrity, plan.outcome.graph):
                click.echo(line)

        if not plan.can_commit(force=force):
            click.echo()
            click.secho("REFUSED — nothing was written:", fg="red")
            for reason in plan.blocked_reasons:
                click.echo(f"  - {reason}")
            if plan.base_is_stale and not plan.schema_errors and not plan.integrity.errors:
                click.echo("Re-run with --force to apply it anyway, or regenerate "
                           "the changeset against the current graph.")
        elif committed is not None:
            click.secho(f"Wrote {repo.graph_path} at revision {committed.id}.",
                        fg="green")

    if not plan.can_commit(force=force):
        sys.exit(1)


@graph.command("context")
@click.option("--for", "purpose", type=click.Choice(["prd-import", "repo-map", "spec-check"]),
              default="prd-import", show_default=True,
              help="What the context is for. Decides which layers the subgraph carries.")
@click.option("--change", "change_id", default=None,
              help="Scope the payload to this change (default: the single open one).")
@click.option("--profile", "profile_path", type=click.Path(exists=True, path_type=Path),
              default=None,
              help="Path to a profile YAML (default: auto-detected under ./.acsdd/profiles).")
@click.option("--capabilities-dir", type=click.Path(path_type=Path), default=None,
              help="Root capabilities directory (default: auto-detected).")
@click.option("--graph-dir", type=click.Path(path_type=Path), default=None,
              help="Graph directory (default: auto-detected .acsdd/graph).")
@click.option("--changes-dir", type=click.Path(path_type=Path), default=None,
              help="Changes directory (default: auto-detected .acsdd/changes).")
@click.option("--max-nodes", type=int, default=None,
              help="Cap the subgraph. An unbounded payload is an unbounded prompt.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the payload as JSON on stdout (what the graph-import skill consumes).")
def graph_context(purpose: str, change_id: Optional[str], profile_path: Optional[Path],
                  capabilities_dir: Optional[Path], graph_dir: Optional[Path],
                  changes_dir: Optional[Path], max_nodes: Optional[int], as_json: bool):
    """Everything an agent needs to write a changeset this repo will accept.

    The node and edge vocabulary with the full allowed-edge matrix, every
    integrity rule and its severity, the changeset format with the exact
    commands to verify one, the profile's agreed facts, the capability catalog,
    and the slice of the existing graph the purpose calls for.

    This is the contract the packaged `graph-import` skill reads. It exists so
    that skill never restates a rule table — restated rules go stale the first
    time the table changes.

    Informational: always exits 0.
    """
    import json as _json
    import yaml as _yaml

    from acsdd.graph.context import DEFAULT_MAX_NODES, build_context

    repo = _graph_repo(graph_dir)
    store = _change_store(changes_dir)
    change_id = _resolve_change_id(store, change_id, as_json=as_json, required=False)

    try:
        current = repo.load()
    except GraphLoadError as exc:
        click.secho(f"ERROR: {exc}", fg="red")
        sys.exit(1)

    change_payload = None
    if change_id and store.exists(change_id):
        try:
            record = store.load_record(change_id)
        except GraphLoadError as exc:
            click.secho(f"ERROR: {exc}", fg="red")
            sys.exit(1)
        applied = store.applied_revision(change_id)
        change_payload = {
            "id": record.id,
            "dir": str(store.change_dir(change_id)),
            "title": record.title,
            "prd_path": record.prd_path,
            "status": "applied" if applied else "open",
            "applied_revision": applied,
            "changeset_path": str(store.changeset_path(change_id)),
            "id_prefix": f"{change_id}.",
        }

    # The profile is optional: a repo can import a PRD before it has one, and
    # the payload says so via profile: null rather than refusing.
    profile_data = None
    if profile_path is None:
        profile_path = _default_profile_path()
        if profile_path is not None:
            click.echo(f"No --profile given, using: {profile_path}", err=as_json)
    if profile_path is not None:
        try:
            loaded = _yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
        except _yaml.YAMLError as exc:
            click.secho(f"ERROR: {profile_path} is not parseable YAML:", fg="red")
            click.echo(f"  - {exc}")
            sys.exit(1)
        profile_data = loaded.get("profile") or {}

    capabilities_dir = capabilities_dir or _default_capabilities_dir()
    manifests_dir = capabilities_dir / MANIFESTS_SUBDIR
    manifests: Dict[str, Dict] = {}
    if manifests_dir.is_dir():
        manifests, load_errors = _load_all(manifests_dir)
        for err in load_errors:
            click.secho(f"WARNING: {err}", fg="yellow", err=True)

    payload = build_context(
        current, str(repo.graph_path), purpose=purpose, change=change_payload,
        profile=profile_data,
        profile_path=str(profile_path) if profile_path else None,
        manifests=manifests, max_nodes=max_nodes or DEFAULT_MAX_NODES)

    if as_json:
        click.echo(_json.dumps({"acsdd_version": __version__, **payload.to_dict()},
                               indent=2, sort_keys=False))
        return

    # A --json-only command would be the first in this tool and would be
    # undebuggable by hand, so there is a human mode — deliberately a summary,
    # since the whole point of the payload is that it is too much to read.
    data = payload.to_dict()
    click.secho(f"Context for '{purpose}'", bold=True)
    click.echo(f"  graph:        {data['graph_path']} "
               f"({data['counts']['nodes']} nodes, {data['counts']['edges']} edges, "
               f"revision {data['graph_revision'] or 'none'})")
    click.echo(f"  change:       {change_id or 'none'}")
    if data["profile"]:
        click.echo(f"  profile:      {data['profile']['id']} "
                   f"({data['profile']['status']}, "
                   f"{'usable' if data['profile']['usable'] else 'NOT usable'})")
    else:
        click.echo("  profile:      none")
    click.echo(f"  capabilities: {len(data['capabilities'])}")
    click.echo(f"  vocabulary:   {len(data['vocabulary']['node_types'])} node types, "
               f"{len(data['vocabulary']['edge_types'])} edge types")
    click.echo(f"  rules:        {len(data['rules'])}")
    click.echo(f"  subgraph:     {data['subgraph']['node_count']} nodes"
               f"{' (truncated)' if data['subgraph']['truncated'] else ''}")
    click.echo()
    click.echo("Re-run with --json for the payload an agent consumes.")


@graph.command("diff")
@click.argument("changeset_path", type=click.Path(exists=True, path_type=Path),
                required=False)
@click.option("--change", "change_id", default=None,
              help="Diff this change's changeset.json (default: the single open one).")
@click.option("--for", "projection", type=click.Choice(["raw", "c4"]), default="raw",
              show_default=True,
              help="'c4' classifies components as new/modified/removed/related.")
@click.option("--graph-dir", type=click.Path(path_type=Path), default=None,
              help="Graph directory (default: auto-detected .acsdd/graph).")
@click.option("--changes-dir", type=click.Path(path_type=Path), default=None,
              help="Changes directory (default: auto-detected .acsdd/changes).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the diff as JSON on stdout.")
def graph_diff(changeset_path: Optional[Path], change_id: Optional[str], projection: str,
               graph_dir: Optional[Path], changes_dir: Optional[Path], as_json: bool):
    """Show what a changeset would do to the graph, without applying it.

    With `--for c4`, classifies every affected component as NEW, MODIFIED,
    REMOVED or RELATED and takes the one-hop closure around them — the input
    the packaged `c4-component-diagram` skill draws from. Those four states are
    computed from the changeset, never stored on a node, so they cannot go
    stale.

    Informational: always exits 0, including when the changeset would be
    refused. `graph apply --dry-run` is the command that tells you that.
    """
    import json as _json

    from acsdd.graph import report as graph_report
    from acsdd.graph.changeset import ChangeSetError, apply_operations
    from acsdd.graph.project_c4 import classification_report
    from acsdd.graph.repository import load_changeset

    repo = _graph_repo(graph_dir)
    store = _change_store(changes_dir)

    if changeset_path is None:
        change_id = _resolve_change_id(store, change_id, as_json=as_json)
        changeset_path = store.changeset_path(change_id)
        if not changeset_path.is_file():
            click.secho(f"ERROR: change '{change_id}' has no changeset at "
                        f"{changeset_path}.", fg="red")
            sys.exit(1)

    try:
        before = repo.load()
        changeset = load_changeset(changeset_path)
    except (ChangeSetError, GraphLoadError) as exc:
        click.secho(f"ERROR: {changeset_path}: {exc}", fg="red")
        sys.exit(1)

    outcome = apply_operations(before, changeset)

    if projection == "c4":
        requirements = sorted(
            op.node.id for op in changeset.operations
            if op.node is not None and op.node.type == "Requirement")
        report = classification_report(outcome, before, requirements=requirements)
        if as_json:
            click.echo(_json.dumps({
                "acsdd_version": __version__,
                "changeset_path": str(changeset_path),
                "changeset_id": changeset.id,
                **report,
            }, indent=2, sort_keys=False))
            return
        for line in graph_report.render_c4_classification(report):
            click.echo(line)
        return

    if as_json:
        click.echo(_json.dumps({
            "acsdd_version": __version__,
            "changeset_path": str(changeset_path),
            "changeset_id": changeset.id,
            **outcome.to_dict(),
        }, indent=2, sort_keys=False))
        return

    for line in graph_report.render_apply_outcome(outcome, None, dry_run=True):
        click.echo(line)


@graph.command("revisions")
@click.option("--graph-dir", type=click.Path(path_type=Path), default=None,
              help="Graph directory (default: auto-detected .acsdd/graph).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the log as JSON on stdout.")
def graph_revisions(graph_dir: Optional[Path], as_json: bool):
    """List the graph's revision history, oldest first."""
    import json as _json

    from acsdd.graph import report as graph_report

    repo = _graph_repo(graph_dir)
    try:
        log = repo.revisions()
    except GraphLoadError as exc:
        click.secho(f"ERROR: {exc}", fg="red")
        sys.exit(1)

    if as_json:
        click.echo(_json.dumps({
            "acsdd_version": __version__,
            "graph_path": str(repo.graph_path),
            "revisions": [r.to_dict() for r in log],
        }, indent=2, sort_keys=False))
        return

    for line in graph_report.render_revisions(log):
        click.echo(line)


# ---------------------------------------------------------------------
# change
# ---------------------------------------------------------------------

@cli.group()
def change():
    """Manage the per-change overlays the graph is edited through.

    A change owns a PRD, the business-layer nodes derived from it, and the
    changeset those become against the repository graph. Keeping them per
    change is what lets the durable graph accumulate engineering and technical
    knowledge across features instead of rediscovering it for every PRD.
    """


@change.command("new")
@click.argument("title")
@click.option("--id", "change_id", default=None,
              help="Change id (default: a slug of TITLE). Becomes the directory name.")
@click.option("--prd", "prd_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="The PRD this change is derived from.")
@click.option("--changes-dir", type=click.Path(path_type=Path), default=None,
              help="Changes directory (default: auto-detected .acsdd/changes).")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite an existing change record with this id.")
def change_new(title: str, change_id: Optional[str], prd_path: Optional[Path],
               changes_dir: Optional[Path], force: bool):
    """Start a change: create .acsdd/changes/<id>/change.json.

    The id also namespaces the change's business-layer node ids
    (req:<id>.<slug>), which is what stops two changes colliding on a name as
    ordinary as "checkout".
    """
    import re as _re
    from datetime import datetime, timezone

    from acsdd.graph.repository import ChangeRecord

    store = _change_store(changes_dir)

    if change_id is None:
        change_id = _re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:64]
    if not _re.match(r"^[a-z0-9][a-z0-9-]{2,63}$", change_id):
        click.secho(f"ERROR: '{change_id}' is not a usable change id.", fg="red")
        click.echo("Ids are lowercase letters, digits and hyphens, 3-64 characters. "
                   "Pass --id to set one explicitly.")
        sys.exit(1)

    record_path = store.record_path(change_id)
    if record_path.exists() and not force:
        click.secho(f"ERROR: change '{change_id}' already exists — re-run with "
                    f"--force to overwrite:", fg="red")
        click.echo(f"  - {record_path}")
        sys.exit(1)

    store.save_record(ChangeRecord(
        id=change_id, title=title,
        created_at=datetime.now(timezone.utc).date().isoformat(),
        prd_path=str(prd_path) if prd_path else None))

    click.secho(f"Created {record_path}", fg="green")
    click.echo()
    click.echo("Next:")
    click.echo(f"  1. acsdd graph context --json --for prd-import --change {change_id}")
    click.echo(f"  2. an agent writes {store.changeset_path(change_id)}")
    click.echo(f"  3. acsdd graph apply --change {change_id} --dry-run")
    if not is_installed("graph-import", Path.cwd()):
        click.echo()
        click.secho("Tip: `acsdd skill install` drops the graph-import skill into "
                    "this repo, which can do step 2 for you.", dim=True)


@change.command("list")
@click.option("--changes-dir", type=click.Path(path_type=Path), default=None,
              help="Changes directory (default: auto-detected .acsdd/changes).")
def change_list(changes_dir: Optional[Path]):
    """List every change in this repository, open ones marked."""
    store = _change_store(changes_dir)
    ids = store.list_ids()

    if not ids:
        click.echo("No changes yet. Start one with `acsdd change new \"Some title\"`.")
        return

    for change_id in ids:
        applied = store.applied_revision(change_id)
        mark = "[x]" if applied else "[ ]"
        try:
            title = store.load_record(change_id).title
        except GraphLoadError:
            title = "(unreadable change.json)"
        suffix = f"   applied as {applied}" if applied else ""
        click.secho(f"  {mark} {change_id}", fg="green" if applied else "yellow")
        click.echo(f"        {title}{suffix}")


@change.command("show")
@click.argument("change_id", required=False)
@click.option("--changes-dir", type=click.Path(path_type=Path), default=None,
              help="Changes directory (default: auto-detected .acsdd/changes).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the change as JSON on stdout.")
def change_show(change_id: Optional[str], changes_dir: Optional[Path], as_json: bool):
    """Show one change: its record, its changeset, and whether it landed."""
    import json as _json

    store = _change_store(changes_dir)
    change_id = _resolve_change_id(store, change_id, as_json=as_json)

    if not store.exists(change_id):
        click.secho(f"ERROR: no change '{change_id}' in {store.changes_dir}.", fg="red")
        sys.exit(1)

    try:
        record = store.load_record(change_id)
    except GraphLoadError as exc:
        click.secho(f"ERROR: {exc}", fg="red")
        sys.exit(1)

    changeset = _load_change_overlay(store, change_id)
    applied = store.applied_revision(change_id)

    if as_json:
        click.echo(_json.dumps({
            "acsdd_version": __version__,
            "change": record.to_dict()["change"],
            "changeset_path": str(store.changeset_path(change_id)),
            "has_changeset": changeset is not None,
            "operation_count": len(changeset.operations) if changeset else 0,
            "applied_revision": applied,
        }, indent=2, sort_keys=False))
        return

    click.secho(f"{record.id}", bold=True)
    click.echo(f"  {record.title}")
    click.echo(f"  created: {record.created_at}")
    if record.prd_path:
        click.echo(f"  prd:     {record.prd_path}")
    click.echo(f"  status:  {'applied as ' + applied if applied else 'open'}")
    if changeset is None:
        click.echo("  changeset: none written yet")
    else:
        click.echo(f"  changeset: {len(changeset.operations)} operation(s), "
                   f"base {changeset.base_revision or 'any'}")


@change.command("remove")
@click.argument("change_id")
@click.option("--changes-dir", type=click.Path(path_type=Path), default=None,
              help="Changes directory (default: auto-detected .acsdd/changes).")
@click.option("--force", is_flag=True, default=False,
              help="Actually delete. Without it, the files are only listed.")
def change_remove(change_id: str, changes_dir: Optional[Path], force: bool):
    """Delete every artifact belonging to CHANGE_ID.

    CHANGE_ID is required and never auto-detected: the read commands guess
    because guessing wrong there costs an error message, and here it costs
    files.

    Removing an applied change does not revert it — the graph already holds
    what it wrote, and `graph.json` is history you undo with git, not with this.
    """
    store = _change_store(changes_dir)
    existing = [p for p in change_artifact_paths(store.changes_dir, change_id)
                if p.exists()]
    if not existing:
        click.secho(f"No artifacts for change '{change_id}' in {store.changes_dir}",
                    fg="red")
        sys.exit(1)

    if store.applied_revision(change_id):
        click.secho(f"NOTE: '{change_id}' has been applied — removing it does not "
                    f"revert the graph.", fg="yellow")

    _require_force_to_remove(existing, force)

    # protect= the changes root: a directory that vanished with its last change
    # would read as "this repo never had a graph". remove_paths prunes only
    # immediate parents, so the emptied <change-id>/ goes and the root stays.
    for path in remove_paths(existing, protect=[store.changes_dir]):
        click.secho(f"Removed {path}", fg="green")


if __name__ == "__main__":
    cli()
