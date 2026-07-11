#!/bin/bash
# Heal CCR's failed Java-truststore seed so Bazel (bb/bbr) trusts the agent proxy.
#
# The Claude Code Remote (CCR) agent proxy re-terminates outbound TLS with its own CA. At
# container boot it seeds a JVM truststore and writes /etc/bazel.bazelrc so Bazel's server
# JVM trusts that CA. That seed RACES dpkg's pending ca-certificates-java trigger — both
# regenerate /etc/ssl/certs/java/cacerts — and often loses: CCR's `update-ca-certificates`
# and `keytool -importkeystore` exit 1, /etc/bazel.bazelrc is never written, and bb/bbr fall
# back to the JDK-embedded cacerts (no proxy CA) -> PKIX TLS failures on fetches. `bazelisk`
# is unaffected because the claude-hook points the *session* bazelrc at the system store,
# which bb/bbr do not read. By the time a session is usable the system store has settled and
# carries the proxy CA, so this completes the step CCR skipped: point every Bazel server JVM
# at it. Root-cause writeup: devinfra/claude/docs/ccr_bazel_truststore_race.md
#
# Idempotent + non-clobbering: writes only when /etc/bazel.bazelrc is absent (CCR writes it on
# a successful seed), so a healthy session — or a machine that isn't a CCR session at all — is
# left untouched.
set -euo pipefail

SYSTEM_BAZELRC=/etc/bazel.bazelrc
JAVA_TRUSTSTORE=/etc/ssl/certs/java/cacerts
CCR_MARKER=/root/.ccr/ca-bundle.crt

log() {
  printf '[%s] heal_ccr_bazel_trust: %s\n' "$(date -Iseconds)" "$*"
}

if [ -e "$SYSTEM_BAZELRC" ]; then
  log "$SYSTEM_BAZELRC already present (healthy seed or prior heal); leaving it untouched."
  exit 0
fi

if [ ! -e "$CCR_MARKER" ]; then
  log "no CCR agent-proxy marker ($CCR_MARKER); not a CCR session — nothing to heal."
  exit 0
fi

if [ ! -r "$JAVA_TRUSTSTORE" ]; then
  log "WARNING: $CCR_MARKER present but $JAVA_TRUSTSTORE is not readable; cannot heal Bazel trust."
  exit 0
fi

cat >"$SYSTEM_BAZELRC" <<EOF
# Managed by devinfra/claude/heal_ccr_bazel_trust.sh.
# CCR's boot-time JVM-truststore seed lost a race with ca-certificates-java and never wrote
# this file, so bb/bbr could not verify the agent proxy's re-terminated TLS. Point every Bazel
# server JVM at the system Java store, which (post-boot) carries the proxy CA.
# See devinfra/claude/docs/ccr_bazel_truststore_race.md.
startup --host_jvm_args=-Djavax.net.ssl.trustStore=$JAVA_TRUSTSTORE
startup --host_jvm_args=-Djavax.net.ssl.trustStorePassword=changeit
EOF
log "wrote $SYSTEM_BAZELRC pointing Bazel's JVM at $JAVA_TRUSTSTORE (CCR truststore-seed heal)."
