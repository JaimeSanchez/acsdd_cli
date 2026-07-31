"""Self-update for the standalone PyInstaller binary install of acsdd.

Only meaningful for the frozen binary produced by `packaging/acsdd.spec`
(installed via `install.sh`) — there's no single executable file to replace
for a source/pip install, so `perform_update` refuses to run unless PyInstaller's
`sys.frozen` marker is set. This mirrors install.sh's own platform-detection,
download, and checksum-verification steps in Python (stdlib only, no `curl`
dependency at runtime) rather than shelling out to re-run that script.
"""

import hashlib
import json
import os
import platform
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

DEFAULT_REPO = "JaimeSanchez/acsdd_cli"
_REQUEST_TIMEOUT = 30


class UpdateError(Exception):
    """Raised for any condition that should abort the update cleanly."""


def is_frozen_binary() -> bool:
    return bool(getattr(sys, "frozen", False))


def detect_platform() -> Tuple[str, str]:
    """Returns (os, arch) using the same naming as install.sh's asset names."""
    os_name = platform.system().lower()
    if os_name not in ("linux", "darwin"):
        raise UpdateError(
            f"unsupported OS: {os_name} — acsdd ships binaries for linux and macOS only."
        )

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        raise UpdateError(f"unsupported architecture: {machine}")

    return os_name, arch


def latest_release_tag(repo: str = DEFAULT_REPO) -> str:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
        data = json.load(resp)
    return data["tag_name"]


def _download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=_REQUEST_TIMEOUT) as resp:
        dest.write_bytes(resp.read())


def verify_checksum(binary_path: Path, checksum_path: Path) -> bool:
    expected_hash = checksum_path.read_text().strip().split()[0]
    actual_hash = hashlib.sha256(binary_path.read_bytes()).hexdigest()
    return expected_hash == actual_hash


def perform_update(repo: str = DEFAULT_REPO, version: Optional[str] = None) -> str:
    """Downloads and atomically replaces the running binary in place.
    Returns the release tag that was installed."""
    if not is_frozen_binary():
        raise UpdateError(
            "acsdd update only works for the standalone binary install. "
            "For a source install, use `pip install --upgrade acsdd` or `git pull`."
        )

    os_name, arch = detect_platform()
    asset = f"acsdd-{os_name}-{arch}"
    try:
        tag = version or latest_release_tag(repo)
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"failed to check latest release: {e}") from e
    base_url = f"https://github.com/{repo}/releases/download/{tag}"

    current_exe = Path(sys.executable).resolve()

    with tempfile.TemporaryDirectory(dir=current_exe.parent) as tmpdir:
        tmp_binary = Path(tmpdir) / asset
        tmp_checksum = Path(tmpdir) / f"{asset}.sha256"
        try:
            _download(f"{base_url}/{asset}", tmp_binary)
            _download(f"{base_url}/{asset}.sha256", tmp_checksum)
        except (urllib.error.URLError, OSError) as e:
            raise UpdateError(f"failed to download {asset} for {tag}: {e}") from e

        if not verify_checksum(tmp_binary, tmp_checksum):
            raise UpdateError("checksum verification failed — aborting update")

        tmp_binary.chmod(0o755)
        os.replace(tmp_binary, current_exe)

    return tag
