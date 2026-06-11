#!/usr/bin/env bash
set -euo pipefail

config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/kitty"
theme_dir="$config_dir/themes"
current_theme="$config_dir/current-theme.conf"

detect_preference() {
  if command -v gsettings >/dev/null 2>&1; then
    local value
    value=$(gsettings get org.gnome.desktop.interface color-scheme 2>/dev/null || true)
    case "$value" in
      *"prefer-dark"*)
        echo "solarized-dark"
        return
        ;;
      *"prefer-light"*)
        echo "solarized-light"
        return
        ;;
    esac
  fi
  echo "solarized-light"
}

apply_link() {
  local theme_name="$1"
  local target="$theme_dir/${theme_name}.conf"
  if [[ -f "$target" ]]; then
    mkdir -p "$config_dir"
    ln -sf "$target" "$current_theme"
  fi
}

notify_kitty() {
  local theme_name="$1"
  local target="$theme_dir/${theme_name}.conf"
  local socket="/tmp/kitty-remote"
  if command -v kitty >/dev/null 2>&1 && [[ -S "$socket" ]]; then
    kitty @ --to "unix:${socket}" set-colors --all "$target" >/dev/null 2>&1 || true
  fi
}

main() {
  local theme
  theme=$(detect_preference)
  apply_link "$theme"
  notify_kitty "$theme"
}

main "$@"
