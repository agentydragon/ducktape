#!/bin/bash
# Bazel-runnable launcher for local iteration on aiquota.
#
# Builds the extension zip via Bazel, extracts it to a temp dir, and launches
# a nested gnome-shell --devkit session with the extension pre-enabled.
# Fully isolated: does not write to ~/.local/share or modify live dconf.
# Requires gnome-shell to be installed on the host (not bundled — local
# iteration only).
#
# Usage:
#   bazelisk run //aiquota/gnome:devkit
#
# Optional env:
#   AI_QUOTA_FIXTURE=/path/to/fixture.json
#       Skip real auth/HTTP and load the indicator from a fixture JSON
#       (same hook used by //aiquota/gnome:test_render).
set -euo pipefail

# --- begin runfiles.bash initialization v3 ---
f=bazel_tools/tools/bash/runfiles/runfiles.bash
source "${RUNFILES_DIR:-/dev/null}/$f" 2>/dev/null \
  || source "$(grep -sm1 "^$f " "${RUNFILES_MANIFEST_FILE:-/dev/null}" | cut -f2- -d' ')" 2>/dev/null \
  || source "$0.runfiles/$f" 2>/dev/null \
  || source "$(dirname "$0").runfiles/$f" 2>/dev/null \
  || {
    echo >&2 "ERROR: cannot find $f"
    exit 1
  }
# --- end runfiles.bash initialization v3 ---

if ! command -v gnome-shell >/dev/null 2>&1; then
  echo "ERROR: gnome-shell not on PATH. Install it on the host first" >&2
  echo "  (e.g. nix shell nixpkgs#gnome-shell, or use a NixOS GNOME session)." >&2
  exit 1
fi

# gnome-shell precedence for extension lookup is ~/.local/share > /etc/profiles
# (home-manager) > XDG_DATA_DIRS, so a hand-installed copy at ~/.local will
# shadow the devkit-extracted version and you'll be debugging stale JS. We
# burned an evening on this once; fail loudly instead.
uuid="aiquota@allegedly.works"
shadow="$HOME/.local/share/gnome-shell/extensions/$uuid"
if [[ -e "$shadow" ]]; then
  echo "ERROR: $shadow exists and would shadow the devkit extension." >&2
  echo "  gnome-shell prefers ~/.local/share over XDG_DATA_DIRS, so devkit JS" >&2
  echo "  would never load. Remove it (or move it aside) and re-run:" >&2
  echo "    rm -rf $shadow" >&2
  exit 1
fi

# --- locate inputs ---------------------------------------------------------
zip_path="$(rlocation "_main/aiquota/gnome/aiquota.zip")"
if [[ ! -f "$zip_path" ]]; then
  echo "ERROR: aiquota.zip not found in runfiles at $zip_path" >&2
  exit 1
fi

# The whole point of devkit is to exercise the in-repo aiquota — both the
# extension *and* the CLI it subprocesses. Resolve the bazel-built py_binary
# launcher and hand it to the extension via AI_QUOTA_BIN so the spawn doesn't
# fall back to whatever `aiquota` happens to be on PATH (typically a stale
# home-manager install of the released wheel).
aiquota_bin="$(rlocation "_main/aiquota/aiquota")"
if [[ ! -x "$aiquota_bin" ]]; then
  echo "ERROR: aiquota py_binary not found in runfiles at $aiquota_bin" >&2
  exit 1
fi

# --- set up isolated temp tree ---------------------------------------------
tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/aiquota-devkit.XXXXXX")
trap 'rm -rf "$tmpdir"' EXIT

# Extract extension to temp data dir.
ext_dir="$tmpdir/data/gnome-shell/extensions/$uuid"
mkdir -p "$ext_dir"
echo ">> extracting $(basename "$zip_path") → $ext_dir"
unzip -q -o "$zip_path" -d "$ext_dir"

# Isolated config dir: symlink everything from real config except dconf,
# which gets its own isolated db via DCONF_PROFILE.
real_conf="${XDG_CONFIG_HOME:-$HOME/.config}"
conf_dir="$tmpdir/config"
mkdir -p "$conf_dir/dconf"
for item in "$real_conf"/*; do
  base=$(basename "$item")
  [[ "$base" == "dconf" ]] && continue
  ln -s "$item" "$conf_dir/$base"
done

# Copy user's dconf db as read-only fallback.
if [[ -f "$real_conf/dconf/user" ]]; then
  cp "$real_conf/dconf/user" "$conf_dir/dconf/user"
fi

# DCONF_PROFILE: writable "devkit" db (auto-created), read-only "user" fallback.
cat >"$tmpdir/dconf-profile" <<'EOF'
user-db:devkit
user-db:user
EOF

# --- set isolated environment for gsettings + gnome-shell -------------------
export XDG_DATA_DIRS="$tmpdir/data${XDG_DATA_DIRS:+:$XDG_DATA_DIRS}"
export XDG_CONFIG_HOME="$conf_dir"
export DCONF_PROFILE="$tmpdir/dconf-profile"

# Wrapper around the bazel-built aiquota so the spawned CLI always reads the
# *real* config.toml regardless of XDG_CONFIG_HOME passthrough. Belt-and-
# suspenders: export AI_QUOTA_BIN (extension.js's preferred path) and also
# prepend a $tmpdir/bin/aiquota symlink to PATH so the spawn's default
# fallback still lands here if env doesn't survive.
real_aiquota_config="$real_conf/aiquota/config.toml"
if [[ ! -e "$real_aiquota_config" ]]; then
  echo "WARN: $real_aiquota_config not found — providers needing config will error" >&2
fi
wrapper="$tmpdir/aiquota-wrapper.sh"
cat >"$wrapper" <<EOF
#!/usr/bin/env bash
exec $(printf %q "$aiquota_bin") --config $(printf %q "$real_aiquota_config") "\$@"
EOF
chmod +x "$wrapper"
mkdir -p "$tmpdir/bin"
ln -sf "$wrapper" "$tmpdir/bin/aiquota"
export AI_QUOTA_BIN="$wrapper"
export PATH="$tmpdir/bin:$PATH"
echo ">> AI_QUOTA_BIN=$AI_QUOTA_BIN"

# --- enable extension in isolated dconf ------------------------------------
gsettings set org.gnome.shell disable-user-extensions false
gsettings set org.gnome.shell enabled-extensions "['$uuid']"
echo ">> enabled $uuid in isolated dconf"

# --- launch the devkit shell ----------------------------------------------
echo ">> launching gnome-shell --devkit (Ctrl-C to exit)"
exec dbus-run-session -- gnome-shell --devkit --wayland
