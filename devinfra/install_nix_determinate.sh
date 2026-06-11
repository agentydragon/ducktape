#!/usr/bin/env bash
# Shared Determinate Nix installer wrapper used by Codex Cloud and Claude web setup.

set -euo pipefail

NIX_INSTALLER_VERSION="v3.17.3"
NIX_INSTALLER_URL="https://github.com/DeterminateSystems/nix-installer/releases/download/${NIX_INSTALLER_VERSION}/nix-installer-x86_64-linux"
NIX_INSTALLER_SHA256="4a84424a0a598b671de21fca1602ea3e74af214d823020afe7aac0056dc032ac"
NIX_INSTALLER_BIN="$(mktemp /tmp/nix-installer.XXXXXX)"

cleanup() {
  rm -f "$NIX_INSTALLER_BIN"
}
trap cleanup EXIT

curl -fsSL "$NIX_INSTALLER_URL" -o "$NIX_INSTALLER_BIN"
echo "${NIX_INSTALLER_SHA256}  ${NIX_INSTALLER_BIN}" | sha256sum -c
chmod +x "$NIX_INSTALLER_BIN"

# Ensure $USER is set — some profile setup paths no-op when it's empty.
saved_user="${USER:-}"
export USER="${USER:-$(id -u -n)}"

"$NIX_INSTALLER_BIN" install linux \
  --no-confirm \
  --init none \
  --extra-conf "sandbox = false" \
  --extra-conf "max-jobs = auto" \
  --extra-conf "system-features =" \
  --extra-conf "substituters = https://cache.nixos.org" \
  --extra-conf "trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="

if [ -z "$saved_user" ]; then
  unset USER
else
  export USER="$saved_user"
fi
