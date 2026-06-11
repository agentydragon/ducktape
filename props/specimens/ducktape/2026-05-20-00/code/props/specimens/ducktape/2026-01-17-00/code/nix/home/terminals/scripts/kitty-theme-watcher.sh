#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="${XDG_CONFIG_HOME:-$HOME/.config}/kitty/bin/kitty-apply-theme.sh"

run_once() {
  if [[ -x "$SCRIPT_PATH" ]]; then
    "$SCRIPT_PATH"
  fi
}

monitor_loop() {
  if command -v gsettings >/dev/null 2>&1; then
    gsettings monitor org.gnome.desktop.interface color-scheme | while read -r _; do
      run_once
    done
  else
    while sleep 60; do
      run_once
    done
  fi
}

run_once
monitor_loop
