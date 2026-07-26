#!/bin/sh
# Install acsdd from a prebuilt release binary.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/JaimeSanchez/acsdd_cli/main/install.sh | sh
#
# Env vars:
#   ACSDD_VERSION      release tag to install, e.g. v0.2.0 (default: latest)
#   ACSDD_INSTALL_DIR  where to put the binary (default: $HOME/.local/bin)
#   ACSDD_REPO         GitHub owner/repo to install from (default: JaimeSanchez/acsdd_cli)

set -e

ACSDD_REPO="${ACSDD_REPO:-JaimeSanchez/acsdd_cli}"
ACSDD_INSTALL_DIR="${ACSDD_INSTALL_DIR:-$HOME/.local/bin}"

os=$(uname -s | tr '[:upper:]' '[:lower:]')
arch=$(uname -m)

case "$arch" in
    aarch64|arm64) arch=arm64 ;;
    x86_64|amd64) arch=x86_64 ;;
    *)
        echo "error: unsupported architecture: $arch" >&2
        echo "acsdd currently ships binaries for linux/macOS on x86_64 and arm64 only." >&2
        exit 1
        ;;
esac

case "$os" in
    linux|darwin) ;;
    *)
        echo "error: unsupported OS: $os" >&2
        echo "acsdd currently ships binaries for linux and macOS only (no Windows build yet)." >&2
        exit 1
        ;;
esac

asset="acsdd-${os}-${arch}"

if [ -n "$ACSDD_VERSION" ]; then
    base_url="https://github.com/${ACSDD_REPO}/releases/download/${ACSDD_VERSION}"
else
    base_url="https://github.com/${ACSDD_REPO}/releases/latest/download"
fi

tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT

echo "Downloading ${asset} from ${ACSDD_REPO}..."
curl -fsSL "${base_url}/${asset}" -o "${tmp_dir}/${asset}"
curl -fsSL "${base_url}/${asset}.sha256" -o "${tmp_dir}/${asset}.sha256"

echo "Verifying checksum..."
(
    cd "$tmp_dir"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum -c "${asset}.sha256"
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 -c "${asset}.sha256"
    else
        echo "warning: no sha256sum/shasum found, skipping checksum verification" >&2
    fi
)

mkdir -p "$ACSDD_INSTALL_DIR"
mv "${tmp_dir}/${asset}" "${ACSDD_INSTALL_DIR}/acsdd"
chmod +x "${ACSDD_INSTALL_DIR}/acsdd"

echo "Installed acsdd to ${ACSDD_INSTALL_DIR}/acsdd"

case ":$PATH:" in
    *":${ACSDD_INSTALL_DIR}:"*) ;;
    *)
        echo ""
        echo "Note: ${ACSDD_INSTALL_DIR} is not on your PATH."
        echo "Add this to your shell profile (e.g. ~/.bashrc or ~/.zshrc):"
        echo "  export PATH=\"${ACSDD_INSTALL_DIR}:\$PATH\""
        ;;
esac
