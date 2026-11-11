#!/bin/bash
# Launch a packaged extension in an isolated nested GNOME Shell.
set -euo pipefail

if ! command -v gnome-shell >/dev/null 2>&1; then
  echo "ERROR: gnome-shell not on PATH. Install it on the host first" >&2
  echo "  (e.g. nix shell nixpkgs#gnome-shell, or use a NixOS GNOME session)." >&2
  exit 1
fi

# gnome-shell precedence for extension lookup is ~/.local/share > /etc/profiles
# (home-manager) > XDG_DATA_DIRS, so a hand-installed copy at ~/.local will
# shadow the devkit-extracted version and you'll be debugging stale JS. We
# burned an evening on this once; fail loudly instead.
uuid="$1"
zip_runfile="$2"
daemon_runfile="$3"
fixture_env="$4"
default_fixture_runfile="$5"
daemon_arg="$6"
host_bus_env="$7"
shift 7

fixture="${!fixture_env:-}"
mode=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --live)
      mode=live
      shift
      ;;
    --host-daemon)
      if [[ "$host_bus_env" == - ]]; then
        echo "ERROR: this extension does not support --host-daemon" >&2
        exit 2
      fi
      mode=host
      shift
      ;;
    --fixture)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --fixture requires a file" >&2
        exit 2
      fi
      mode=fixture
      fixture="$2"
      shift 2
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done
if [[ "$host_bus_env" != - ]]; then
  unset "$host_bus_env"
fi
if [[ "$mode" == host ]]; then
  if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
    echo "ERROR: --host-daemon requires the host session D-Bus address" >&2
    exit 1
  fi
  printf -v "$host_bus_env" '%s' "$DBUS_SESSION_BUS_ADDRESS"
  export "$host_bus_env"
fi
if [[ "$mode" == live || "$mode" == host ]]; then
  fixture=""
elif [[ -z "$fixture" && "$default_fixture_runfile" != - ]]; then
  fixture="$(rlocation "$default_fixture_runfile")"
fi
if [[ "$mode" == fixture && -z "$fixture" ]]; then
  echo "ERROR: --fixture requires a file" >&2
  exit 2
fi
if [[ -n "$fixture" ]]; then
  if [[ "$fixture" != /* ]]; then
    fixture="${BUILD_WORKING_DIRECTORY:-$PWD}/$fixture"
  fi
  if [[ ! -f "$fixture" ]]; then
    echo "ERROR: fixture not found: $fixture" >&2
    exit 1
  fi
  printf -v "$fixture_env" '%s' "$fixture"
  export "$fixture_env"
else
  unset "$fixture_env"
fi
shadow="$HOME/.local/share/gnome-shell/extensions/$uuid"
if [[ -e "$shadow" ]]; then
  echo "ERROR: $shadow exists and would shadow the devkit extension." >&2
  echo "  gnome-shell prefers ~/.local/share over XDG_DATA_DIRS, so devkit JS" >&2
  echo "  would never load. Remove it (or move it aside) and re-run:" >&2
  echo "    rm -rf $shadow" >&2
  exit 1
fi

# --- locate inputs ---------------------------------------------------------
zip_path="$(rlocation "$zip_runfile")"
if [[ ! -f "$zip_path" ]]; then
  echo "ERROR: extension zip not found in runfiles at $zip_path" >&2
  exit 1
fi

# Resolve the checkout daemon for live mode.
daemon_bin="$(rlocation "$daemon_runfile")"
if [[ ! -x "$daemon_bin" ]]; then
  echo "ERROR: desktop daemon not found in runfiles at $daemon_bin" >&2
  exit 1
fi

# --- set up isolated temp tree ---------------------------------------------
tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/gnome-devkit.XXXXXX")
trap 'rm -rf "$tmpdir"' EXIT

# Extract extension to temp data dir.
ext_dir="$tmpdir/data/gnome-shell/extensions/$uuid"
mkdir -p "$ext_dir"
echo ">> extracting $(basename "$zip_path") → $ext_dir"
unzip -q -o "$zip_path" -d "$ext_dir"
if [[ -d "$ext_dir/schemas" ]]; then
  glib-compile-schemas "$ext_dir/schemas"
fi

# Isolated config dir: symlink everything from real config except dconf,
# which gets its own isolated db via DCONF_PROFILE.
real_conf="${XDG_CONFIG_HOME:-$HOME/.config}"
conf_dir="$tmpdir/config"
mkdir -p "$conf_dir/dconf"
for item in "$real_conf"/*; do
  [[ -e "$item" ]] || continue
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
dbus-run-session -- bash -c '
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

  if [[ "$3" == live ]]; then
    if [[ "$4" == - ]]; then
      "$2" &
    else
      "$2" "$4" &
    fi
    daemon_pid=$!
  fi

  runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  nested_wayland_name="gnome-devkit-$$"
  suffix=0
  while [[ -e "$runtime_dir/$nested_wayland_name" || -e "$runtime_dir/$nested_wayland_name.lock" ]]; do
    suffix=$((suffix + 1))
    nested_wayland_name="gnome-devkit-$$-$suffix"
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
' sh "['$uuid']" "$daemon_bin" "$([[ -n "$fixture" ]] && printf fixture || printf '%s' "${mode:-live}")" "$daemon_arg"
