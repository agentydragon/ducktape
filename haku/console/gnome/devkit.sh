#!/usr/bin/env bash
# Build and launch the standalone Haku Approvals GTK/WebKit app directly.
#
# The app no longer needs a nested GNOME Shell: its GTK window is a normal
# Wayland application. The script still rebuilds the local Bazel artifact so
# it tests current source rather than the last released Nix pin.
set -euo pipefail

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_directory/../../.." && pwd)"
cd "$repo_root"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'EOF'
Usage: ./haku/console/gnome/devkit.sh

Builds the Haku Approvals package from the local Bazel artifact and launches
the standalone GTK/WebKit application in the current graphical session.

Set HAKU_CONSOLE_URL to point the app at another Haku Console origin.
EOF
  exit 0
fi

for command in bbr nix; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "ERROR: $command is not on PATH. Load the repo's Nix devshell first." >&2
    exit 1
  fi
done

echo ">> building //haku/console/gnome:haku_approvals_zip"
bbr build //haku/console/gnome:haku_approvals_zip
readonly artifact_path="$(readlink -f bb-out/bazel-out/k8-fastbuild/bin/haku/console/gnome/haku-approvals.zip)"
if [[ ! -f "$artifact_path" ]]; then
  echo "ERROR: Bazel did not produce $artifact_path." >&2
  exit 1
fi

echo ">> building .#hakuApprovals from the local artifact"
readonly artifact_overrides="{\"haku-approvals\":\"$artifact_path\"}"
readonly package_path="$(DUCKTAPE_ARTIFACT_OVERRIDES="$artifact_overrides" nix build --impure .#hakuApprovals --no-link --print-out-paths)"
if [[ ! -x "$package_path/bin/haku-approvals" ]]; then
  echo "ERROR: hakuApprovals output is missing bin/haku-approvals." >&2
  exit 1
fi

# Prefer the existing host Wayland compositor. In shells launched from a
# terminal or IDE, WAYLAND_DISPLAY can be absent even though the compositor's
# socket is available; without this, GTK may try X11 and fail on a Wayland-only
# session with an authorization or display-protocol error.
runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ -z "${WAYLAND_DISPLAY:-}" && -S "$runtime_dir/wayland-0" ]]; then
  export WAYLAND_DISPLAY=wayland-0
fi
if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
  export GDK_BACKEND="${GDK_BACKEND:-wayland}"
  echo ">> using host Wayland display $WAYLAND_DISPLAY"
fi
# Wyrm2's Mutter advertises explicit sync but rejects WebKitGTK's dmabuf
# surface when it is first committed. Use WebKitGTK's shared-memory path; the
# package wrapper applies the same default to normal desktop launches.
export WEBKIT_DISABLE_DMABUF_RENDERER="${WEBKIT_DISABLE_DMABUF_RENDERER:-1}"

echo ">> launching local Haku Approvals"
exec env \
  PATH="$package_path/bin:$PATH" \
  HAKU_APPLICATION_ID="${HAKU_APPLICATION_ID:-works.allegedly.HakuApprovals.Dev}" \
  haku-approvals --show
