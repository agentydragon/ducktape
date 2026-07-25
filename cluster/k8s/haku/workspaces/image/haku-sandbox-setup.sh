#!/usr/bin/env bash
# Per-claim setup for a Haku sandbox: egress CA trust, git identity + credentials, and the
# haku-state checkout. Invoked (post-adoption, once per claim) by the sandbox-provisioning
# MCP's bootstrap.script, which is just a call to this file — the whole reviewed bootstrap
# lives here rather than as a bash blob inside the MCP's config YAML, so shfmt/shellcheck
# see it. Deviation: changing any of it now needs an image rebuild + rollout, where the
# in-YAML version was a ConfigMap edit picked up on the next claim. Accepted — the MCP's
# environment contract hash never covered the image tag either way
# (haku/sandbox_mcp/config.py), so no drift detection is lost.
#
# Idempotent: safe to re-run against an already-set-up box.
set -euo pipefail

# ── 1. Egress CA at the JVM level ────────────────────────────────────────────
# Bazel's downloader runs in the server JVM, which validates against a Java KeyStore and
# ignores SSL_CERT_FILE. The proxy bumps every host, so a store holding only the egress CA
# validates bcr.bazel.build / GitHub / npm / PyPI alike. The CA bundle is mounted by the
# Kyverno egress injection at $EGRESS_CA. (Adapted from haku-state tools/ci/trust_egress_ca.sh.)
bundle="${EGRESS_CA:-/egress-proxy-ca/ca-certificates.crt}"
store="$HOME/egress-truststore.p12"
pass="changeit"
egress_cn="haku-egress-proxy-root-ca"

if [ -r "$bundle" ]; then
  tmp="$(mktemp -d)"
  csplit -sz -f "$tmp/c" -b '%02d.pem' "$bundle" '/-----BEGIN CERTIFICATE-----/' '{*}'
  rm -f "$store"
  certs=()
  for f in "$tmp"/c*.pem; do
    openssl x509 -noout -subject -in "$f" 2>/dev/null | grep -q "$egress_cn" && certs+=("$f")
  done
  [ "${#certs[@]}" -eq 0 ] && certs=("$tmp"/c*.pem) # fallback: import all (slow, correct)
  for f in "${certs[@]}"; do
    keytool -importcert -noprompt -storepass "$pass" -storetype PKCS12 \
      -keystore "$store" -alias "egress-$(basename "$f" .pem)" -file "$f"
  done
  cat >"$HOME/.bazelrc" <<RC
startup --host_jvm_args=-Djavax.net.ssl.trustStore=$store
startup --host_jvm_args=-Djavax.net.ssl.trustStorePassword=$pass
startup --host_jvm_args=-Djavax.net.ssl.trustStoreType=PKCS12
build --disk_cache=$HOME/.cache/bazel-disk
RC
else
  echo "haku-sandbox-setup: egress CA bundle not found at $bundle — bazel fetches may fail TLS" >&2
fi

# ── 2. Git identity ──────────────────────────────────────────────────────────
# Without this the first `git commit` of a run dies "Author identity unknown".
git config --global user.name haku
git config --global user.email haku@allegedly.works

# ── 3. Git credentials ───────────────────────────────────────────────────────
# The haku Forgejo account (HAKU_GIT_* from the haku-forgejo-git secret, via the
# SandboxTemplate) reaches the SAME Forgejo under two hostnames, and both are needed:
# forgejo-http.forgejo in-cluster (the ducktape_haku module git_override on every bazel
# invocation + the haku-state clone below) and the public git.allegedly.works, which the
# CLI's REST readers use — `haku read --source cpap` 404s unauthenticated against it.
# (Collapsing the two names is tracked in haku/TODO.md.)
umask 077
cat >"$HOME/.netrc" <<NETRC
machine forgejo-http.forgejo
login ${HAKU_GIT_USERNAME:?HAKU_GIT_USERNAME unset}
password ${HAKU_GIT_PASSWORD:?HAKU_GIT_PASSWORD unset}
machine git.allegedly.works
login ${HAKU_GIT_USERNAME}
password ${HAKU_GIT_PASSWORD}
NETRC

# ── 4. haku-state checkout ───────────────────────────────────────────────────
# So `bazel run //cli:...` has something to run against. Branch is main (haku-state's
# default), not master.
#
# --depth 1: forgejo's full-history pack generation is the dominant cost (a full clone runs
# minutes even with the egress proxy bypassed; a shallow clone is ~9s), and the sandbox only
# builds/runs the HEAD checkout — it needs no history. Deviation: if a build step ever needs
# git history/tags (e.g. `git describe` version stamping), deepen the fetch here.
repo="${HAKU_STATE_DIR:-/workspace/haku-state}"
url="${HAKU_STATE_URL:-http://forgejo-http.forgejo:3000/haku/haku-state.git}"
if [ -d "$repo/.git" ]; then
  git -C "$repo" fetch --depth 1 --prune origin main
  git -C "$repo" checkout -B main origin/main
  git -C "$repo" reset --hard origin/main
else
  git clone --depth 1 --branch main --single-branch "$url" "$repo"
fi

echo "haku-sandbox-setup: egress CA trusted, git identity + credentials written, haku-state synced"
