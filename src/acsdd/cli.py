"""acsdd — command-line tool for the ACSDD framework.

Subcommand groups:
  acsdd capability   validate / list / show / graph
  acsdd catalog      build / verify
  acsdd profile      discover / validate
"""

import sys
from pathlib import Path
from typing import Dict, Optional

import click

from acsdd import __version__
from acsdd.capability.loader import iter_manifests, ManifestLoadError
from acsdd.capability.validator import validate_manifest, validate_catalog
from acsdd.catalog.builder import build_catalog_markdown
from acsdd.profile.validator import validate_profile_file


def _default_capabilities_dir() -> Path:
    """Walk up from cwd looking for a capabilities/_manifests dir, falling
    back to ./capabilities. Keeps the CLI usable from any subdirectory of
    a project that has adopted the ACSDD capabilities/ layout."""
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / "capabilities" / "_manifests").is_dir():
            return candidate / "capabilities"
    return cwd / "capabilities"


@click.group()
@click.version_option(version=__version__)
def cli():
    """ACSDD — AI-Collaborative Software Development & Delivery CLI."""


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


@capability.command("validate")
@click.argument("path", type=click.Path(exists=True, path_type=Path), required=False)
@click.option("--manifests-dir", type=click.Path(path_type=Path), default=None,
              help="Directory of manifests (default: auto-detected capabilities/_manifests).")
def capability_validate(path: Optional[Path], manifests_dir: Optional[Path]):
    """Validate one manifest (PATH) or every manifest in --manifests-dir
    against the Appendix A schema, plus cross-manifest dependency checks."""
    manifests_dir = manifests_dir or _default_capabilities_dir() / "_manifests"

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

    if not manifests_dir.is_dir():
        click.secho(f"No manifests directory found at {manifests_dir}", fg="red")
        sys.exit(1)

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
    manifests_dir = manifests_dir or _default_capabilities_dir() / "_manifests"
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
    manifests_dir = manifests_dir or _default_capabilities_dir() / "_manifests"
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


# ---------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------

@cli.group()
def catalog():
    """Build and verify the capability catalog (CATALOG.md)."""


@catalog.command("build")
@click.option("--capabilities-dir", type=click.Path(path_type=Path), default=None,
              help="Root capabilities/ directory (default: auto-detected).")
@click.option("--out", type=click.Path(path_type=Path), default=None,
              help="Output path (default: <capabilities-dir>/CATALOG.md).")
def catalog_build(capabilities_dir: Optional[Path], out: Optional[Path]):
    """Regenerate CATALOG.md from the manifests in _manifests/. This file
    is generated — don't hand-edit it, since a rebuild will overwrite it."""
    capabilities_dir = capabilities_dir or _default_capabilities_dir()
    manifests_dir = capabilities_dir / "_manifests"
    out = out or capabilities_dir / "CATALOG.md"

    manifests, load_errors = _load_all(manifests_dir)
    for err in load_errors:
        click.secho(f"WARN  {err}", fg="yellow")

    md = build_catalog_markdown(manifests, docs_root=capabilities_dir, manifests_root=manifests_dir)
    out.write_text(md, encoding="utf-8")
    click.secho(f"Wrote {out} ({len(manifests)} capabilities)", fg="green")


@catalog.command("verify")
@click.option("--capabilities-dir", type=click.Path(path_type=Path), default=None)
def catalog_verify(capabilities_dir: Optional[Path]):
    """Fail if CATALOG.md is stale relative to the manifests, or if any
    manifest fails schema/cross-manifest validation. Intended for CI."""
    capabilities_dir = capabilities_dir or _default_capabilities_dir()
    manifests_dir = capabilities_dir / "_manifests"
    catalog_path = capabilities_dir / "CATALOG.md"

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
    """Discover and validate ACSDD Engineering Profiles."""


@profile.command("discover")
@click.argument("repo_path", type=click.Path(exists=True, path_type=Path))
@click.option("--profile-id", required=True)
@click.option("--depth", default="deep", type=click.Choice(["surface", "deep"]))
@click.option("--known-stack", default=None)
@click.option("--target-version", default="0.2.0")
@click.option("--output", default="./acsdd/profiles")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite existing draft/report/recommendations files")
def profile_discover(repo_path, profile_id, depth, known_stack, target_version, output, force):
    """Run PROFILE-001 discovery against REPO_PATH (wraps the existing
    profile-discovery implementation)."""
    from acsdd.profile._discovery_impl import main as discovery_main

    argv = [str(repo_path), "--profile-id", profile_id, "--depth", depth,
            "--target-version", target_version, "--output", output]
    if known_stack:
        argv += ["--known-stack", known_stack]
    if force:
        argv += ["--force"]
    discovery_main(argv)


@profile.command("validate")
@click.argument("profile_path", type=click.Path(exists=True, path_type=Path))
def profile_validate(profile_path: Path):
    """Validate a Profile YAML file against the Appendix B schema."""
    result = validate_profile_file(profile_path)
    if result.ok:
        click.secho(f"PASS  {profile_path}", fg="green")
        return
    click.secho(f"FAIL  {profile_path}", fg="red")
    for err in result.errors:
        click.echo(f"  - {err}")
    sys.exit(1)


if __name__ == "__main__":
    cli()
