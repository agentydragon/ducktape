#!/bin/bash
# Setup script for Claude Code web sessions.
#
# Installs:
#   1. Nix (Determinate Systems installer, designed for non-interactive/CI use)
#   2. devtools — Rust claude-hook, statusline, bbapi, gh, skills; from flake via attic binary cache
#   3. git remote + bbr config for BuildBuddy remote execution
#   4. skills — symlinked per-skill into ~/.claude/skills/ (preserves Anthropic defaults)
#   5. user-level ~/.bazelrc with a shared local --disk_cache (web sessions only)
#
# Also reclaims ~90% of the root ext4's nobody-reserved blocks up front (the
# container ships with ~84% reserved; we run as root, so it's pure overhead).
#
# Secrets are NOT decrypted here. SOPS_AGE_KEY is a user UI env var and is
# only available to Claude Code and its subprocesses — not to this setup script.
# This script is run directly by environment-manager as a bash init script
# (process.ExecuteScript → temp file "init-script-*.sh"), inheriting only the
# container env. `claude --init-only` (which fires Setup+SessionStart hooks) runs
# separately afterward. Neither step receives user UI vars.
# Decryption happens exclusively in the claude-hook daemon via startup_env_script.
#
# IMPORTANT: This script always exits 0 so the session starts even if setup
# fails. Failures are logged to /tmp/web-setup.log.
#
# Set DUCKTAPE_WEB_SETUP_UPLOAD_LOG=1 to upload the log to ix.io on completion
# (useful for debugging sessions where you can't read the log directly).
#
# Usage (Claude Code web UI setup command):
#   bash ducktape/devinfra/claude/web_setup.sh
#
# Historical --impl=<python|rust> arguments are accepted for compatibility, but
# active sessions always install the Rust claude-hook implementation.
#
# CLEANUP(added 2026-04-19): this script runs TWICE per session — once as the
# init script (no user-UI env vars, always installs python default) and
# once via the Setup hook (web_setup_hook.sh, has UI env vars). Once the Setup hook
# is confirmed to fire reliably across all session modes (new / resume
# / resume-cached / setup-only — see devinfra/claude/README.md) and is
# sufficient on its own, make the init-script path a no-op (or drop it
# from the Claude Code web UI "Setup Command" field) and let the Setup
# hook own devtools install. Remove this tombstone after ≥1 week of
# live sessions with no "Setup hook missed firing" fallbacks needed.

LOG_FILE="/tmp/web-setup.log"
exec > >(tee -a "$LOG_FILE") 2>&1

set -euo pipefail

log() {
  printf '[%s] %s\n' "$(date -Iseconds)" "$*"
}

warn() {
  log "WARNING: $*" >&2
}

# Hook implementation is Rust-only. Keep parsing the old selector so stale web
# UI env vars or setup args do not break session startup.
HOOK_IMPL="rust"
for arg in "$@"; do
  case "$arg" in --impl=*) warn "Ignoring deprecated $arg; claude-hook is Rust-only" ;; esac
done
if [ -n "${DUCKTAPE_CLAUDE_HOOK_IMPL:-}" ] && [ "${DUCKTAPE_CLAUDE_HOOK_IMPL:-}" != "rust" ]; then
  warn "Ignoring deprecated DUCKTAPE_CLAUDE_HOOK_IMPL=$DUCKTAPE_CLAUDE_HOOK_IMPL; claude-hook is Rust-only"
fi
DEVTOOLS_OUTPUT="devtools"

FLAKE="path:$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SETUP_COMMIT=$(git -C "${FLAKE#path:}" rev-parse HEAD 2>/dev/null || echo 'unknown')
log "web_setup.sh commit: $SETUP_COMMIT"

# --- Step 0: Reclaim root-reserved ext4 blocks ---
# The web container's root ext4 ships with ~84% of blocks reserved for
# nobody:nogroup (tune2fs default is 5%), leaving only ~41 GiB of the 256 GiB
# disk usable — see <web_env/docs/container_spec.md>. We run as root (UID 0),
# so those blocks are pure overhead, not a safety margin. Free ~90% of the
# reservation (keep 10%) before the disk-hungry Nix install runs.
#
# Idempotent: skipped once the reservation is already low, so the per-session
# re-runs (persistent rootfs) don't keep shrinking it toward zero.
reclaim_reserved_blocks() {
  command -v tune2fs >/dev/null 2>&1 || {
    warn "tune2fs not found; skipping reserved-block reclaim"
    return 0
  }
  local dev fstype
  dev=$(findmnt -no SOURCE / 2>/dev/null || echo /dev/vda)
  fstype=$(findmnt -no FSTYPE / 2>/dev/null || echo unknown)
  if [ "$fstype" != "ext4" ]; then
    warn "root fs is '$fstype' (not ext4); skipping reserved-block reclaim"
    return 0
  fi
  local info total reserved
  info=$(tune2fs -l "$dev" 2>/dev/null) || {
    warn "could not read tune2fs -l $dev; skipping reserved-block reclaim"
    return 0
  }
  total=$(awk -F: '/^Block count:/ {gsub(/ /,"",$2); print $2}' <<<"$info")
  reserved=$(awk -F: '/^Reserved block count:/ {gsub(/ /,"",$2); print $2}' <<<"$info")
  if [ -z "$total" ] || [ -z "$reserved" ] || [ "$total" -eq 0 ]; then
    warn "could not parse block counts from $dev; skipping reserved-block reclaim"
    return 0
  fi
  local pct=$((reserved * 100 / total))
  if [ "$pct" -le 20 ]; then
    log "Reserved blocks already low (${pct}% of ${total}); skipping reclaim."
    return 0
  fi
  local target=$((reserved / 10)) # keep ~10%, free ~90%
  log "Reclaiming reserved blocks on ${dev}: ${reserved} (${pct}%) -> ${target}"
  if tune2fs -r "$target" "$dev" >/dev/null 2>&1; then
    log "Reserved block count set to ${target} on ${dev}."
  else
    warn "tune2fs -r failed on ${dev}; reserved blocks unchanged."
  fi
}
reclaim_reserved_blocks

# --- Step 1: Install Nix (Determinate Systems installer) ---
# Uses the Determinate Systems installer instead of the official one because:
#   - Designed for CI/non-interactive environments (no TTY assumptions)
#   - Single static binary — no shell script wrapping nix-env -i
#   - Avoids the "reading a line: Input/output error" bug where the official
#     installer's nix-env tries to read from a TTY that doesn't exist
#   - --init none skips systemd/launchd (not available in containers)
# Pinned binary from GitHub releases, SHA256-verified.
log "Installing Nix (Determinate installer)..."
bash "${FLAKE#path:}/devinfra/install_nix_determinate.sh"
# shellcheck disable=SC1091
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
log "Nix installation complete."

# --- Step 2: Install web session tools ---
# Debug: dump environment keys for proxy/cert diagnostics (no values — avoids logging JWTs).
log "--- environment keys ---"
env | cut -d= -f1 | sort
log "---"

log "Connectivity check (cache.nixos.org)..."
curl -fsSL --max-time 10 https://cache.nixos.org/nix-cache-info || warn "cache.nixos.org check failed — continuing anyway"

log "--- nix.conf ---"
cat /etc/nix/nix.conf
log "--- nix show-config ---"
nix show-config 2>/dev/null | grep -E "max-jobs|sandbox|build-users" || true
log "---"

log "Installing web session tools..."
log "  FLAKE=${FLAKE}"
log "  pwd=$(pwd)"
log "  flake.nix exists: $(test -f flake.nix && echo yes || echo no)"
log "  ls pwd:"
ls -la
# Pass --max-jobs explicitly to override any nix.conf misconfiguration.
#
# CRITICAL: `nix profile install` is a no-op when the attribute path is already
# in the profile — it matches by attrpath, not by evaluated store hash. On
# persistent Firecracker rootfs `web_setup.sh` re-runs on every session
# (environment-manager's `Initialize` fires `init_script` unconditionally —
# verified in <devinfra/claude/web_env/re/environment_manager/src/internal/envtype/anthropic/anthropic.go>
# Initialize(), Step 3 at line ~361 is not gated on session mode). Without an
# explicit remove, the installed devtools derivation freezes at whatever pin
# was current on container first-boot, even though `nix/artifact-pins.json` in
# the working tree has moved forward. The downstream symptom is agent sessions
# running a stale devtools profile against fresh profile YAML / hook behavior,
# which crashes SessionStart as soon as the schema churns. Remove
# first so `install` re-evaluates `.#devtools` against the current flake.
#
# See <devinfra/claude/docs/web-setup-debug.md> ("Pin drift on persistent rootfs").
nix profile remove devtools 2>/dev/null || true
nix profile remove devtools-rust 2>/dev/null || true
# TODO: `attic login` against cache.allegedly.works/{main,gaffer} before this
# install so devtools (and any gaffer-built closures pulled transitively) hit
# the private cache instead of building from source. The reader JWT is in
# secrets/claude-web-attic.yaml, auto-rotated by the in-cluster
# attic-jwt-rotation CronJob; web_env.sh decrypts SOPS files at hook-daemon
# startup but isn't running yet at this point in init_script. Sketch:
#   if [ -f /tmp/_secret_attic_token ]; then
#     attic login allegedly https://cache.allegedly.works \
#       "$(cat /tmp/_secret_attic_token)"
#   fi
# Plumbing needed: have web_env.sh write the decrypted token to a known
# path (mode 0600, owned by user) before init_script runs, OR fold the
# sops decrypt into web_setup.sh directly using SOPS_AGE_KEY. Today the
# install path falls back to building from source if main isn't reachable
# anonymously — slow but works.
nix profile install --max-jobs auto "${FLAKE}#${DEVTOOLS_OUTPUT}"
log "Dev tools installed (impl=$HOOK_IMPL)."

# Symlink all Nix-installed binaries into /usr/local/bin so they're on PATH.
# Claude Code is launched directly (not via login shell), so the Nix profile bin
# is not in PATH when hooks run. /usr/local/bin is always in PATH.
NIX_PROFILE="/nix/var/nix/profiles/default"
for bin in "${NIX_PROFILE}"/bin/*; do
  ln -sfn "$bin" /usr/local/bin/"$(basename "$bin")"
done

PROJECT_DIR="${FLAKE#path:}"

# --- Step 3: Add github-no-proxy remote for bbr ---
# Claude Code web sessions use a local git proxy as 'origin'
# (http://127.0.0.1:<port>/git/...). When 'bb remote' sends a RunRequest to
# the BuildBuddy cloud runner, it embeds the selected remote's URL in
# RepoState.repo.url. The runner then fetches from that URL. The local proxy
# URL is unreachable from the cloud runner, so bbr fails.
#
# Fix: add a 'github-no-proxy' remote that points directly at GitHub.
#
# bb source: https://github.com/buildbuddy-io/buildbuddy (cli/remotebazel/)
#
# bb auto-selects a single remote without prompting. With 2+ remotes, in TTY
# mode it shows a selection TUI; in non-TTY mode (pre-commit hooks, CI) it
# reads buildbuddy.remote-bazel-remote-name and uses the named remote directly.
# Value must be a remote NAME (not a URL) — bb resolves the URL from git config.
GITHUB_REMOTE_URL="https://github.com/agentydragon/ducktape"
if git -C "$PROJECT_DIR" remote get-url github-no-proxy &>/dev/null; then
  log "git remote 'github-no-proxy' already exists, skipping."
else
  git -C "$PROJECT_DIR" remote add github-no-proxy "$GITHUB_REMOTE_URL"
  log "Added git remote 'github-no-proxy' -> $GITHUB_REMOTE_URL"
fi
git -C "$PROJECT_DIR" config buildbuddy.remote-bazel-remote-name github-no-proxy
log "Set buildbuddy.remote-bazel-remote-name=github-no-proxy"

# --- Step 4: Symlink skills into ~/.claude/skills/ ---
# Per-skill symlinks instead of replacing the directory, so Anthropic's
# pre-landed default skills are preserved.
log "Deploying skills to ~/.claude/skills/..."
mkdir -p ~/.claude/skills
for skill in "${NIX_PROFILE}"/share/claude-hooks/skills/*/; do
  ln -sfn "$skill" ~/.claude/skills/"$(basename "$skill")"
done

# --- Step 5: User-level Bazel disk cache (web sessions only) ---
# A single local --disk_cache shared by every Bazel server instance and worktree
# in the container. Claude Code web runs on a persistent Firecracker rootfs, so
# this cache survives session boundaries and speeds up local `bazelisk` / `bb run`
# action execution. (bbr/RBE work is cached remotely on BuildBuddy and is
# unaffected — disk_cache and remote cache coexist.)
#
# Installed at ~/.bazelrc (Bazel's home rc): the bazelisk/bazel shims inject the
# session bazelrc via an extra --bazelrc= but do NOT pass --nohome_rc, so Bazel
# still reads this file. The home rc is read before the session --bazelrc, and no
# other layer sets --disk_cache, so this wins.
#
# Web-only by construction: this script runs only in web sessions. CLI sessions
# on local machines already configure their own shared disk cache via
# home-manager (see <devinfra/docs/bazel_worktree_cache_sharing.md>), so we must
# not clobber it there — and we don't, because this code path never runs on them.
#
# GC bounds keep the cache bounded; Step 0 reclaims most of the root-reserved
# blocks, so ~50 GiB is comfortable on the 256 GiB root disk
# (see <web_env/docs/container_spec.md>).
BAZEL_DISK_CACHE="${HOME}/.cache/bazel/disk"
mkdir -p "$BAZEL_DISK_CACHE"
HOME_BAZELRC="${HOME}/.bazelrc"
BAZELRC_BEGIN="# >>> ducktape web disk cache >>>"
BAZELRC_END="# <<< ducktape web disk cache <<<"
# Drop any prior managed block, then re-append a fresh one so re-runs (every
# session, per the persistent-rootfs note above) stay idempotent.
if [ -f "$HOME_BAZELRC" ]; then
  sed -i "/$BAZELRC_BEGIN/,/$BAZELRC_END/d" "$HOME_BAZELRC"
fi
cat >>"$HOME_BAZELRC" <<EOF
$BAZELRC_BEGIN
# Managed by devinfra/claude/web_setup.sh — do not edit by hand.
build --disk_cache=${BAZEL_DISK_CACHE}
build --experimental_disk_cache_gc_max_size=50G
build --experimental_disk_cache_gc_max_age=7d
$BAZELRC_END
EOF
log "Installed user-level Bazel disk cache: ${BAZEL_DISK_CACHE} (${HOME_BAZELRC})"

log "Setup complete. Log: ${LOG_FILE}"

if [ "${DUCKTAPE_WEB_SETUP_UPLOAD_LOG:-0}" = "1" ]; then
  UPLOAD_URL=$(curl -s -F "f:1=@${LOG_FILE}" ix.io 2>/dev/null || echo "upload failed")
  log "Log uploaded: ${UPLOAD_URL}"
fi
