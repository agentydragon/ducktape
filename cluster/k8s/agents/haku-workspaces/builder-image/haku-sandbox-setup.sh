#!/usr/bin/env bash
# Per-claim runtime setup for a Haku sandbox, invoked by the sandbox-provisioning
# MCP's bootstrap script (post-adoption) before any `bazel` exec. Idempotent.
#
# Two things the baked image can't carry because they're cluster-runtime material:
#   1. Trust the egress-proxy ssl-bump CA at the JVM level. Bazel's downloader runs
#      in the server JVM, which validates against a Java KeyStore and ignores
#      SSL_CERT_FILE. The proxy bumps every host, so a store holding only the egress
#      CA validates bcr.bazel.build / GitHub / npm / PyPI alike. The CA bundle is
#      mounted by the Kyverno egress injection at $EGRESS_CA. (Adapted from
#      haku-state tools/ci/trust_egress_ca.sh; keytool is baked here, so no
#      bazel-install-base hunting.)
#   2. Git credentials for the in-cluster Forgejo, from env secrets wired by the
#      SandboxTemplate (never baked into the image):
#        - DUCKTAPE_MIRROR_READ_TOKEN: the ducktape_haku git_override fetches
#          http://forgejo-http.forgejo:3000/haku/ducktape.git during bzlmod
#          resolution on EVERY bazel invocation.
#        - HAKU_STATE_READ_TOKEN: to clone/pull haku-state itself.
#      Both are Forgejo tokens owned by tf/gitops/haku-state (never hand-minted).
set -euo pipefail

bundle="${EGRESS_CA:-/egress-proxy-ca/ca-certificates.crt}"
store="$HOME/egress-truststore.p12"
pass="changeit"
egress_cn="haku-egress-proxy-root-ca"

# --- 1. JVM truststore for the Bazel downloader ---------------------------------
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

# --- 2. Git credentials for the in-cluster Forgejo ------------------------------
git config --global credential.helper store
: >"$HOME/.git-credentials"
chmod 600 "$HOME/.git-credentials"
if [ -n "${DUCKTAPE_MIRROR_READ_TOKEN:-}" ]; then
  printf 'http://haku:%s@forgejo-http.forgejo:3000\n' "$DUCKTAPE_MIRROR_READ_TOKEN" >>"$HOME/.git-credentials"
fi
if [ -n "${HAKU_STATE_READ_TOKEN:-}" ]; then
  printf 'http://haku:%s@forgejo-http.forgejo:3000\n' "$HAKU_STATE_READ_TOKEN" >>"$HOME/.git-credentials"
fi

echo "haku-sandbox-setup: ready"
