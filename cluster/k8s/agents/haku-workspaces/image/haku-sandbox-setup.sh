#!/usr/bin/env bash
# Trust the egress-proxy ssl-bump CA at the JVM level, for a Haku sandbox. Invoked
# by the sandbox-provisioning MCP's bootstrap (post-adoption) before any `bazel`.
# Idempotent.
#
# This is the ONE piece of per-claim setup that must be baked into the image: it
# shells out to the baked `keytool`. Everything git (the .netrc + the haku-state
# clone) is image-independent and lives in the MCP bootstrap.script instead, so it
# can change without an image rebuild.
#
# Why the JVM store: Bazel's downloader runs in the server JVM, which validates
# against a Java KeyStore and ignores SSL_CERT_FILE. The proxy bumps every host, so
# a store holding only the egress CA validates bcr.bazel.build / GitHub / npm / PyPI
# alike. The CA bundle is mounted by the Kyverno egress injection at $EGRESS_CA.
# (Adapted from haku-state tools/ci/trust_egress_ca.sh.)
set -euo pipefail

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

echo "haku-sandbox-setup: egress CA trusted"
