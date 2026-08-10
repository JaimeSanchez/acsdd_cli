"""acsdd — command-line tool for the ACSDD framework.

Subcommand groups:
  acsdd capability   validate / list / show / generate / recommend / remove
  acsdd catalog      build / verify
  acsdd profile      discover / validate / create / review / remove
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
from acsdd.paths import (
    ACSDD_DIR,
    DEFAULT_PROFILES_DIR,
    MANIFESTS_SUBDIR,
    profile_artifact_paths,
    resolve_capabilities_dir,
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


if __name__ == "__main__":
    cli()
