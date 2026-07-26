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

# ── 2b. Git TLS trust ────────────────────────────────────────────────────────
# git links against a system OpenSSL that reads neither SSL_CERT_FILE nor CURL_CA_BUNDLE
# (both of which the Kyverno egress injection sets, and which curl does honor). The proxy
# bumps every external host, so without this git sees a cert chain it can't build and dies
# `server certificate verification failed. CAfile: none` — while `curl https://github.com/`
# beside it returns 200, which makes the failure read like a network block rather than a
# trust-store gap. Measured 2026-07-25: setting this is the whole difference between
# `git ls-remote https://github.com/...` failing and returning devel's HEAD.
if [ -r "$bundle" ]; then
  git config --global http.sslCAInfo "$bundle"
fi

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

# ── 5. ducktape checkout (Haku's read-only base) ─────────────────────────────
# `haku/run.md` step 2 (base-sync) diffs `haku/base` + `haku/run.md` between the pin in
# haku-state's memory/base-sync.md and ducktape HEAD. Until 2026-07-25 no ducktape existed
# here at all, so a sandbox-hosted run was structurally incapable of that step and had to
# borrow the harness's checkout. Cloning it closes that, and doubles as the way an ad-hoc
# claude.ai chat (which has no ducktape) can read Haku's manual at all.
#
# From GitHub, not the in-cluster Forgejo mirror: the mirror is not yet auto-synced and was
# measured 3 commits behind devel (97a23895 vs a4c497f7) on 2026-07-25, and base-sync against
# a stale HEAD silently under-reports contract changes. Public repo, so no credential — the
# egress proxy allows github.com and §2b just taught git to trust it.
#
# --filter=blob:none, NOT --depth 1: base-sync needs real history to resolve `<pin>..HEAD`,
# which a shallow clone cannot do. A partial clone keeps every commit and fetches blobs on
# demand — measured 11s / ~102 MB, against minutes for a full clone.
if [ "${HAKU_DUCKTAPE_SKIP:-}" != "1" ]; then
  dt_repo="${HAKU_DUCKTAPE_DIR:-/workspace/ducktape}"
  dt_url="${HAKU_DUCKTAPE_URL:-https://github.com/agentydragon/ducktape.git}"
  dt_branch="${HAKU_DUCKTAPE_BRANCH:-devel}"
  # Non-fatal: ducktape is read-only reference. A run whose base-sync is unavailable should
  # degrade to "surface a finding and carry on", not fail to get a sandbox at all.
  if [ -d "$dt_repo/.git" ]; then
    git -C "$dt_repo" fetch --prune origin "$dt_branch" \
      && git -C "$dt_repo" checkout -B "$dt_branch" "origin/$dt_branch" \
      && git -C "$dt_repo" reset --hard "origin/$dt_branch" \
      || echo "haku-sandbox-setup: ducktape refresh failed — base-sync unavailable this claim" >&2
  else
    git clone --filter=blob:none --single-branch --branch "$dt_branch" "$dt_url" "$dt_repo" \
      || echo "haku-sandbox-setup: ducktape clone failed — base-sync unavailable this claim" >&2
  fi
fi

echo "haku-sandbox-setup: egress CA trusted, git identity + credentials written, haku-state + ducktape synced"
