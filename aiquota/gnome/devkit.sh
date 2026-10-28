#!/bin/bash
# Bazel-runnable launcher for local iteration on aiquota.
#
# Builds the extension zip via Bazel, extracts it to a temp dir, and launches
# a nested gnome-shell --devkit session with the extension pre-enabled. The
# display setup is explicit because Wyrm2 is a Wayland VM with multiple DRM
# render nodes; letting Mutter auto-select a display or renderer produces a
# blank/black devkit surface.
# Fully isolated: does not write to ~/.local/share or modify live dconf.
# Requires gnome-shell to be installed on the host (not bundled — local
# iteration only).
#
# Usage:
#   bazelisk run //aiquota/gnome:devkit
#
# Optional env:
#   AI_QUOTA_FIXTURE=/path/to/fixture.json
#       Skip real auth/HTTP and load the indicator from an API-shaped quota
#       view JSON payload (same hook used by //aiquota/gnome:test_render).
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

# Resolve the in-repo desktop daemon so the extension and live API client use
# this checkout while running inside the isolated D-Bus session.
aiquota_daemon_bin="$(rlocation "_main/aiquota/desktop_bin")"
if [[ ! -x "$aiquota_daemon_bin" ]]; then
  echo "ERROR: aiquota desktop daemon not found in runfiles at $aiquota_daemon_bin" >&2
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

# Mutter's devkit viewer is a client of the existing desktop compositor. When
# launched from a terminal on Wayland, WAYLAND_DISPLAY is often unset and GTK
# falls back to an X11 viewer, which is the blank-screen failure mode seen on
# Wyrm2. Keep the producer and viewer on the same software EGL path as well:
# Wyrm2 exposes a virtio render node plus two NVIDIA nodes, and Mutter's
# automatic multi-GPU choice is not reliable for nested framebuffer sharing.
runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ -z "${WAYLAND_DISPLAY:-}" && -S "$runtime_dir/wayland-0" ]]; then
  export WAYLAND_DISPLAY=wayland-0
fi
if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
  export GDK_BACKEND="${GDK_BACKEND:-wayland}"
  echo ">> using host Wayland display $WAYLAND_DISPLAY"
fi
export GSK_RENDERER="${GSK_RENDERER:-gl}"
export LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-1}"
export MESA_LOADER_DRIVER_OVERRIDE="${MESA_LOADER_DRIVER_OVERRIDE:-llvmpipe}"
export __NV_DISABLE_EXPLICIT_SYNC="${__NV_DISABLE_EXPLICIT_SYNC:-1}"

# --- launch the devkit shell ----------------------------------------------
echo ">> launching gnome-shell --devkit (Ctrl-C to exit)"
exec dbus-run-session -- bash -c '
  set -eu
  daemon_pid=""
  shell_pid=""
  cleanup() {
    trap - EXIT INT TERM
    if [[ -n "$shell_pid" ]] && kill -0 "$shell_pid" 2>/dev/null; then
      kill "$shell_pid" 2>/dev/null || true
    fi
    if [[ -n "$daemon_pid" ]] && kill -0 "$daemon_pid" 2>/dev/null; then
      kill "$daemon_pid" 2>/dev/null || true
    fi
    if [[ -n "$daemon_pid" ]]; then wait "$daemon_pid" 2>/dev/null || true; fi
  }
  trap cleanup EXIT INT TERM
  gsettings set org.gnome.shell disable-user-extensions false
  gsettings set org.gnome.shell enabled-extensions "$1"

  "$2" &
  daemon_pid=$!

  runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  nested_wayland_name="aiquota-$$"
  suffix=0
  while [[ -e "$runtime_dir/$nested_wayland_name" || -e "$runtime_dir/$nested_wayland_name.lock" ]]; do
    suffix=$((suffix + 1))
    nested_wayland_name="aiquota-$$-$suffix"
  done

  gnome-shell --devkit --unsafe-mode --wayland --wayland-display="$nested_wayland_name" &
  shell_pid=$!

  for _ in $(seq 1 300); do
    if [[ -S "$runtime_dir/$nested_wayland_name" ]]; then
      break
    fi
    if ! kill -0 "$shell_pid" 2>/dev/null; then
      wait "$shell_pid"
      exit 1
    fi
    sleep 0.1
  done

  if [[ ! -S "$runtime_dir/$nested_wayland_name" ]]; then
    echo "ERROR: nested Mutter Wayland socket did not appear" >&2
    exit 1
  fi

  echo ">> nested Wayland display is $nested_wayland_name"
  wait "$shell_pid"
' sh "['aiquota@allegedly.works']" "$aiquota_daemon_bin"
