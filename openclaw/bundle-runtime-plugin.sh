#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 GATEWAY_ROOT PLUGIN_ID PLUGIN_SOURCE" >&2
  exit 64
fi

gateway_root=$1
plugin_id=$2
plugin_source=$3

if [[ ! -d "$gateway_root" ]]; then
  echo "gateway package root does not exist: $gateway_root" >&2
  exit 1
fi
if [[ ! -f "$plugin_source/openclaw.plugin.json" ]]; then
  echo "plugin manifest does not exist: $plugin_source/openclaw.plugin.json" >&2
  exit 1
fi

bundled_parent="$gateway_root/dist/extensions"
runtime_parent="$gateway_root/dist-runtime/extensions"
for parent in "$bundled_parent" "$runtime_parent"; do
  if [[ ! -d "$parent" ]]; then
    echo "gateway bundled-plugin root does not exist: $parent" >&2
    exit 1
  fi
  chmod u+w "$parent"
done

bundled_target="$bundled_parent/$plugin_id"
runtime_target="$runtime_parent/$plugin_id"
for target in "$bundled_target" "$runtime_target"; do
  if [[ -e "$target" || -L "$target" ]]; then
    chmod -R u+w "$target" 2>/dev/null || true
    rm -rf "$target"
  fi
done

# Nix store sources are read-only. Make the copied tree writable before replacing
# the package's host dependency with a relative link back to this gateway.
cp -R "$plugin_source" "$bundled_target"
chmod -R u+w "$bundled_target"

host_dependency="$bundled_target/node_modules/openclaw"
if [[ ! -L "$host_dependency" ]]; then
  echo "plugin host dependency is not a symlink: $host_dependency" >&2
  exit 1
fi
rm "$host_dependency"
ln -s ../../../.. "$host_dependency"
# Gotcha: this outward link is what trips the gateway's startup warning
#   [channels] failed to load persistedAuthState checker for matrix:
#   plugin module path escapes plugin root or fails alias checks
# The loader's in-root path check rejects the channel plugin's tiny
# persistedAuthState probe module because it resolves through this link back to
# the gateway. Benign: the full channel plugin still loads and persists its own
# auth/sync state; only the cheap pre-load auth fast-path (doctor/status/presence
# probes) is skipped. Leave it — a fix means giving up this host-dependency link,
# which the bundling relies on.

# OpenClaw selects one of these bundled roots based on package layout. Populate
# both, but hard-link the regular files so the plugin payload is stored once.
cp -al "$bundled_target" "$runtime_target"

for target in "$bundled_target" "$runtime_target"; do
  test -f "$target/openclaw.plugin.json"
  test "$(readlink -f "$target/node_modules/openclaw")" = "$gateway_root"
done
