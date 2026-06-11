#!/usr/bin/env bash
# Dump actual Starship and p10k prompts (left/right) with raw ANSI and escaped forms
# Usage: ./dump_prompts.sh [repo_dir]
set -Eeuo pipefail

REPO_DIR=${1:-$PWD}
# Load direnv environment for target dir so VIRTUAL_ENV/etc are present
if command -v direnv >/dev/null 2>&1; then
  DENV=$(cd "$REPO_DIR" && direnv export bash 2>/dev/null || true)
  eval "$DENV"
fi

esc2hex() { perl -pe 's/\e/\\x1b/g'; }

section() { printf "\n==== %s ====\n" "$1"; }
show() {
  local title="$1"
  shift
  local s="$*"
  section "$title (raw)"
  printf '%s\n' "$s"
  section "$title (escaped)"
  printf '%s\n' "$(printf '%s' "$s" | esc2hex)"
}

# --- Starship ---
STAR_L=$(cd "$REPO_DIR" && starship prompt 2>/dev/null || true)
STAR_R=$(cd "$REPO_DIR" && (starship prompt --right 2>/dev/null || starship prompt --right-prompt 2>/dev/null) || true)

# --- p10k --- use minimal ZDOTDIR so ~/.zshrc defaults don’t interfere
TMP_ZD=$(mktemp -d)
trap 'rm -rf "$TMP_ZD"' EXIT
cat >"$TMP_ZD/.zshrc" <<'EORC'
export ZSH="$HOME/.oh-my-zsh"
export ZSH_THEME="powerlevel10k/powerlevel10k"
source "$ZSH/oh-my-zsh.sh"
[[ -r "$HOME/.p10k.zsh" ]] && source "$HOME/.p10k.zsh"
EORC

# Capture p10k left/right from a login+interactive zsh using this ZDOTDIR
P10K_ALL=$(ZDOTDIR="$TMP_ZD" zsh -lic "cd \"$REPO_DIR\"; print -P \"$PROMPT\"; print -P \"$RPROMPT\"" 2>/dev/null || true)
P10K_L=$(printf '%s\n' "$P10K_ALL" | sed -n '1p')
P10K_R=$(printf '%s\n' "$P10K_ALL" | sed -n '2p')

# Output in quick-scan format with ESC escaped
# Normalize starship output by stripping zsh non-printing wrappers %{ %}
STAR_L_ESC=$(printf '%s' "$STAR_L" | sed -e 's/%{//g' -e 's/%}//g' | esc2hex)
STAR_R_ESC=$(printf '%s' "$STAR_R" | sed -e 's/%{//g' -e 's/%}//g' | esc2hex)
P10K_L_ESC=$(printf '%s' "$P10K_L" | esc2hex)
P10K_R_ESC=$(printf '%s' "$P10K_R" | esc2hex)

printf 'starship x\n%s\n\n' "$STAR_L_ESC"
printf 'p10k x\n%s\n\n' "$P10K_L_ESC"
printf 'starship y\n%s\n\n' "$STAR_R_ESC"
printf 'p10k y\n%s\n' "$P10K_R_ESC"

# Exit nonzero if theme missing so callers can detect
if [[ "$P10K_L" == P10K_THEME_NOT_FOUND* ]]; then
  echo "(p10k theme not found; edit script to point to your powerlevel10k.zsh)" >&2
  exit 2
fi
