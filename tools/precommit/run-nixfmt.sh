#!/usr/bin/env bash
# Pre-commit hook wrapper for nixfmt using a static binary from GitHub releases.
# Downloads the binary on first use and caches it in the pre-commit environment.
# TODO: Support non-Linux platforms (macOS/aarch64) — currently only ships linux-x86_64.
set -euo pipefail

NIXFMT_VERSION="1.2.0"
NIXFMT_SHA256="aa43e06e2e98d07f9393a8dc73978e0df79bad15d7f84fcd838e15557eb056ee"
NIXFMT_URL="https://github.com/NixOS/nixfmt/releases/download/v${NIXFMT_VERSION}/nixfmt"

# Cache directory: use XDG_CACHE_HOME if set, otherwise ~/.cache
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/nixfmt-bin"
NIXFMT_BIN="${CACHE_DIR}/nixfmt-${NIXFMT_VERSION}"

if [[ ! -x "$NIXFMT_BIN" ]]; then
  mkdir -p "$CACHE_DIR"
  TMP="$(mktemp "${CACHE_DIR}/nixfmt-download.XXXXXX")"
  trap 'rm -f "$TMP"' EXIT

  echo "Downloading nixfmt v${NIXFMT_VERSION}..." >&2
  curl -fsSL -o "$TMP" "$NIXFMT_URL"

  ACTUAL_SHA256="$(sha256sum "$TMP" | cut -d' ' -f1)"
  if [[ "$ACTUAL_SHA256" != "$NIXFMT_SHA256" ]]; then
    echo "ERROR: nixfmt checksum mismatch" >&2
    echo "  expected: $NIXFMT_SHA256" >&2
    echo "  got:      $ACTUAL_SHA256" >&2
    exit 1
  fi

  chmod +x "$TMP"
  mv "$TMP" "$NIXFMT_BIN"
  trap - EXIT
  echo "Cached nixfmt v${NIXFMT_VERSION} at ${NIXFMT_BIN}" >&2
fi

exec "$NIXFMT_BIN" "$@"
