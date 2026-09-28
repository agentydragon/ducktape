#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${WAYLAND_DISPLAY:-}" ]]; then
  echo "WAYLAND_DISPLAY is not set; run this from a Wayland desktop session." >&2
  exit 1
fi

system_sw=/run/current-system/sw
kwin="$system_sw/bin/kwin_wayland"
plasma="$system_sw/bin/startplasma-wayland"
default_keyboard="$system_sw/bin/plasma-keyboard"
keyboard="${NESTED_PLASMA_INPUTMETHOD:-$default_keyboard}"
socket="${NESTED_PLASMA_SOCKET:-nested-plasma-osk}"
test_app="${NESTED_PLASMA_TEST_APP:-$system_sw/bin/kwrite}"

bins=("$kwin" "$plasma" "$keyboard")
if [[ -n "$test_app" ]]; then
  bins+=("$test_app")
fi

for bin in "${bins[@]}"; do
  if [[ ! -x "$bin" ]]; then
    echo "Missing executable: $bin" >&2
    echo "Run: sudo nixos-rebuild switch --flake ~/code/ducktape#rugged" >&2
    exit 1
  fi
done

width="${NESTED_PLASMA_WIDTH:-1280}"
height="${NESTED_PLASMA_HEIGHT:-800}"

export KWIN_IM_SHOW_ALWAYS="${KWIN_IM_SHOW_ALWAYS:-1}"

cmd=(
  dbus-run-session -- "$kwin"
  --socket "$socket"
  --wayland-display "$WAYLAND_DISPLAY"
  --xwayland
  --width "$width"
  --height "$height"
  --inputmethod "$keyboard"
  --no-lockscreen
  --no-global-shortcuts
  --no-kactivities
  --exit-with-session "$plasma"
)

if [[ -n "$test_app" ]]; then
  cmd+=("$test_app")
fi

exec "${cmd[@]}"
