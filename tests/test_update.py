"""Tests for acsdd.update (the `acsdd update` self-update command) and its
CLI wiring."""

import hashlib
import os
import stat

import pytest
from click.testing import CliRunner

from acsdd import update as update_mod
from acsdd.cli import cli


def test_detect_platform_maps_known_combinations(monkeypatch):
    cases = [
        ("Linux", "x86_64", ("linux", "x86_64")),
        ("Linux", "aarch64", ("linux", "arm64")),
        ("Darwin", "arm64", ("darwin", "arm64")),
        ("Darwin", "x86_64", ("darwin", "x86_64")),
    ]
    for system, machine, expected in cases:
        monkeypatch.setattr(update_mod.platform, "system", lambda s=system: s)
        monkeypatch.setattr(update_mod.platform, "machine", lambda m=machine: m)
        assert update_mod.detect_platform() == expected


def test_detect_platform_rejects_unsupported_os(monkeypatch):
    monkeypatch.setattr(update_mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(update_mod.platform, "machine", lambda: "AMD64")
    with pytest.raises(update_mod.UpdateError, match="unsupported OS"):
        update_mod.detect_platform()


def test_detect_platform_rejects_unsupported_arch(monkeypatch):
    monkeypatch.setattr(update_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(update_mod.platform, "machine", lambda: "i386")
    with pytest.raises(update_mod.UpdateError, match="unsupported architecture"):
        update_mod.detect_platform()


def test_verify_checksum_true_for_matching_hash(tmp_path):
    binary = tmp_path / "acsdd-linux-x86_64"
    binary.write_bytes(b"fake binary content")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    checksum = tmp_path / "acsdd-linux-x86_64.sha256"
    checksum.write_text(f"{digest}  acsdd-linux-x86_64\n")
    assert update_mod.verify_checksum(binary, checksum) is True


def test_verify_checksum_false_for_mismatched_hash(tmp_path):
    binary = tmp_path / "acsdd-linux-x86_64"
    binary.write_bytes(b"fake binary content")
    checksum = tmp_path / "acsdd-linux-x86_64.sha256"
    checksum.write_text("0" * 64 + "  acsdd-linux-x86_64\n")
    assert update_mod.verify_checksum(binary, checksum) is False


def test_perform_update_raises_when_not_frozen():
    assert update_mod.is_frozen_binary() is False
    with pytest.raises(update_mod.UpdateError, match="pip install --upgrade"):
        update_mod.perform_update()


def test_perform_update_downloads_verifies_and_replaces_binary(monkeypatch, tmp_path):
    fake_exe = tmp_path / "acsdd"
    fake_exe.write_bytes(b"OLD BINARY")
    fake_exe.chmod(0o755)

    monkeypatch.setattr(update_mod.sys, "executable", str(fake_exe))
    monkeypatch.setattr(update_mod, "is_frozen_binary", lambda: True)
    monkeypatch.setattr(update_mod, "detect_platform", lambda: ("linux", "x86_64"))

    new_content = b"NEW BINARY CONTENT"

    def fake_download(url, dest):
        if url.endswith(".sha256"):
            digest = hashlib.sha256(new_content).hexdigest()
            dest.write_text(f"{digest}  acsdd-linux-x86_64\n")
        else:
            dest.write_bytes(new_content)

    monkeypatch.setattr(update_mod, "_download", fake_download)

    tag = update_mod.perform_update(repo="someone/acsdd_cli", version="v9.9.9")

    assert tag == "v9.9.9"
    assert fake_exe.read_bytes() == new_content
    assert fake_exe.stat().st_mode & stat.S_IXUSR


def test_perform_update_aborts_on_checksum_mismatch(monkeypatch, tmp_path):
    fake_exe = tmp_path / "acsdd"
    fake_exe.write_bytes(b"OLD BINARY")

    monkeypatch.setattr(update_mod.sys, "executable", str(fake_exe))
    monkeypatch.setattr(update_mod, "is_frozen_binary", lambda: True)
    monkeypatch.setattr(update_mod, "detect_platform", lambda: ("linux", "x86_64"))

    def fake_download(url, dest):
        if url.endswith(".sha256"):
            dest.write_text("0" * 64 + "  acsdd-linux-x86_64\n")
        else:
            dest.write_bytes(b"NEW BINARY CONTENT")

    monkeypatch.setattr(update_mod, "_download", fake_download)

    with pytest.raises(update_mod.UpdateError, match="checksum"):
        update_mod.perform_update(repo="someone/acsdd_cli", version="v9.9.9")

    # original binary untouched on failure
    assert fake_exe.read_bytes() == b"OLD BINARY"


def test_update_command_reports_error_when_not_frozen():
    runner = CliRunner()
    result = runner.invoke(cli, ["update"])
    assert result.exit_code != 0
    assert "pip install --upgrade" in result.output
