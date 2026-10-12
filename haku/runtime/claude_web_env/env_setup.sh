#!/bin/bash
# Haku Claude Code web environment setup script — paste verbatim into the
# environment's "Setup script" dialog.
#
# Self-contained: this runs BEFORE any repo is cloned, so it reads no ducktape
# path and clones nothing. It is an inlined copy of devinfra/claude/web_setup.sh
# (profile mode) plus devinfra/install_nix_determinate.sh,
# nix/attic-pubkeys.json, devinfra/claude/reconcile_bbr_remote.sh and
# devinfra/claude/heal_ccr_bazel_trust.sh; keep in sync with those files. The
# flake is fetched by plain git (DUCKTAPE_FLAKE_REF), not via the GitHub API.
LOG_FILE="/tmp/web-setup.log"
exec > >(tee -a "$LOG_FILE") 2>&1
set -euo pipefail
log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*"; }
warn() { log "WARNING: $*" >&2; }

WEB_SETUP_OUTPUT="${DUCKTAPE_WEB_SETUP_OUTPUT:-agent-haku}"
FLAKE="${DUCKTAPE_FLAKE_REF:-git+https://github.com/agentydragon/ducktape?ref=devel&shallow=1}"
log "env setup (inlined web_setup.sh): FLAKE=$FLAKE OUTPUT=$WEB_SETUP_OUTPUT"

# --- Step 1: Install Nix (Determinate installer; inlined install_nix_determinate.sh) ---
if [ -x /nix/var/nix/profiles/default/bin/nix ] && /nix/var/nix/profiles/default/bin/nix --version >/dev/null 2>&1; then
  log "Nix already installed ($(/nix/var/nix/profiles/default/bin/nix --version)); skipping installer."
else
  log "Installing Nix (Determinate installer)..."
  NIX_INSTALLER_VERSION="v3.17.3"
  NIX_INSTALLER_SHA256="4a84424a0a598b671de21fca1602ea3e74af214d823020afe7aac0056dc032ac"
  NIX_INSTALLER_BIN="$(mktemp /tmp/nix-installer.XXXXXX)"
  curl -fsSL "https://github.com/DeterminateSystems/nix-installer/releases/download/${NIX_INSTALLER_VERSION}/nix-installer-x86_64-linux" -o "$NIX_INSTALLER_BIN"
  echo "${NIX_INSTALLER_SHA256}  ${NIX_INSTALLER_BIN}" | sha256sum -c
  chmod +x "$NIX_INSTALLER_BIN"
  USER="${USER:-$(id -u -n)}" "$NIX_INSTALLER_BIN" install linux \
    --no-confirm --init none \
    --extra-conf "sandbox = false" \
    --extra-conf "max-jobs = auto" \
    --extra-conf "system-features =" \
    --extra-conf "substituters = https://cache.nixos.org" \
    --extra-conf "trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
  rm -f "$NIX_INSTALLER_BIN"
fi
unset __ETC_PROFILE_NIX_SOURCED
# shellcheck disable=SC1091
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
log "Nix installation complete."

curl -fsSL --max-time 10 https://cache.nixos.org/nix-cache-info || warn "cache.nixos.org check failed — continuing anyway"

# --- Attic substituter (cache.allegedly.works); pubkeys inlined from nix/attic-pubkeys.json ---
mkdir -p /nix/var/determinate
[ -f /nix/var/determinate/netrc ] || (umask 077 && : >/nix/var/determinate/netrc)
if ! grep -q 'cache\.allegedly\.works' /etc/nix/nix.custom.conf 2>/dev/null; then
  cat >>/etc/nix/nix.custom.conf <<'EOF'

# Attic substituter (added by env setup script; reader JWT lands in the netrc via web_env.sh)
netrc-file = /nix/var/determinate/netrc
extra-substituters = https://cache.allegedly.works/public https://cache.allegedly.works/main
extra-trusted-public-keys = main:W4FKePyV8sQL66PpOnh54fAbuuSPKpgXPS4mHAp77Zg= gaffer:JJZtWNWK6mSoKoBkBTI4997S2/5nBj2NC4rLt1I7TDQ= public:BIhpTQ8uN+noKrwtlNBgSXBouH+NY0XycBXKbWdk0z0=
fallback = true
EOF
  log "Attic substituter configured."
fi

# --- Step 2: Install tools from the remote flake ---
nix profile remove devtools 2>/dev/null || true
nix profile remove agent-haku 2>/dev/null || true
NIX_PROFILE="/nix/var/nix/profiles/default"
nix --extra-experimental-features 'nix-command flakes' profile install --refresh --max-jobs auto "${FLAKE}#${WEB_SETUP_OUTPUT}"
log "Dev tools installed (${FLAKE}#${WEB_SETUP_OUTPUT})."
for link in /usr/local/bin/*; do
  [ -L "$link" ] && [ ! -e "$link" ] && rm -f "$link"
done
for bin in "${NIX_PROFILE}"/bin/*; do
  ln -sfn "$bin" /usr/local/bin/"$(basename "$bin")"
done

# --- Step 3: bbr remote reconciliation — SKIPPED ---
# reconcile_bbr_remote needs the ducktape git checkout, which does not exist
# yet. The Setup hook (web_setup_hook.sh -> devinfra/claude/web_setup.sh, run
# from the checkout) performs it later.
log "Skipping bbr remote reconciliation (no checkout at env-setup time; Setup hook does it)."

# --- Step 3b: Heal CCR Java truststore for Bazel (inlined heal_ccr_bazel_trust.sh) ---
if [ ! -e /etc/bazel.bazelrc ] && [ -e /root/.ccr/ca-bundle.crt ] && [ -r /etc/ssl/certs/java/cacerts ]; then
  cat >/etc/bazel.bazelrc <<'EOF'
# Managed by the Haku env setup script (copy of devinfra/claude/heal_ccr_bazel_trust.sh).
# See devinfra/claude/docs/ccr_bazel_truststore_race.md.
startup --host_jvm_args=-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts
startup --host_jvm_args=-Djavax.net.ssl.trustStorePassword=changeit
EOF
  log "wrote /etc/bazel.bazelrc (CCR truststore-seed heal)."
fi

# --- Step 4: Symlink skills into ~/.claude/skills/ ---
mkdir -p ~/.claude/skills
for skill in "${NIX_PROFILE}"/share/claude-hooks/skills/*/; do
  [ -d "$skill" ] && ln -sfn "$skill" ~/.claude/skills/"$(basename "$skill")"
done

# --- Step 5: User-level Bazel disk cache ---
BAZEL_DISK_CACHE="${HOME}/.cache/bazel/disk"
mkdir -p "$BAZEL_DISK_CACHE"
HOME_BAZELRC="${HOME}/.bazelrc"
BEGIN="# >>> ducktape web disk cache >>>"
END="# <<< ducktape web disk cache <<<"
[ -f "$HOME_BAZELRC" ] && sed -i "/$BEGIN/,/$END/d" "$HOME_BAZELRC"
cat >>"$HOME_BAZELRC" <<EOF
$BEGIN
# Managed by devinfra/claude/web_setup.sh — do not edit by hand.
build --disk_cache=${BAZEL_DISK_CACHE}
build --experimental_disk_cache_gc_max_size=50G
build --experimental_disk_cache_gc_max_age=7d
$END
EOF
log "Setup complete. Log: ${LOG_FILE}"
