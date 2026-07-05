#!/usr/bin/env bash
# Build a Java truststore the Bazel server-JVM downloader trusts, containing the
# haku-egress-proxy CA. JSSE validates against a Java KeyStore, not
# SSL_CERT_FILE/CURL_CA_BUNDLE (which already cover curl/git/python/node via the
# runner's /etc/ssl/certs mount). The proxy ssl-bumps EVERY host, so Bazel never
# sees a real upstream leaf — a store holding just the egress CA as a trust anchor
# validates bcr.bazel.build / GitHub / nodejs.org / npm / PyPI alike; the public
# roots are unnecessary. Then point the server JVM at it via the runner's
# ~/.bazelrc (the standard home bazelrc Bazel reads automatically) — machine-local
# config that never touches the checked-in .bazelrc, so local/CLI builds are
# unaffected. The runner's on-disk cache path (also machine-specific) goes here too.
#
# Loud on failure by design: a silent 0-cert import previously left the JVM
# pointing at an empty store and Bazel died with an opaque PKIX error at fetch time.
set -euo pipefail

bundle="${EGRESS_CA:-/egress-proxy-ca/ca-certificates.crt}"
store="${EGRESS_TRUSTSTORE:-$HOME/egress-truststore.p12}"
pass="changeit"

[ -r "$bundle" ] || {
  echo "egress CA bundle not found at $bundle" >&2
  exit 1
}

# keytool ships with a JDK but the job image (catthehacker/ubuntu:act-latest)
# carries none. Bazel's release binary bundles a JDK for its own server, so after
# extracting it (bazel version) keytool lives under the install base — locate it
# there. Search a system JDK first in case the image ever gains one.
keytool="$(command -v keytool || true)"
if [ -z "$keytool" ]; then
  for d in "${JAVA_HOME:-}/bin" /usr/lib/jvm/*/bin /opt/*/bin /usr/local/*/bin; do
    [ -x "$d/keytool" ] && keytool="$d/keytool" && break
  done
fi
if [ -z "$keytool" ]; then
  # Unpack Bazel's install base (bundled JDK) to find keytool. --batch leaves no
  # persistent server, so the real `bazel test` below starts fresh with the
  # truststore --host_jvm_args (a server started here with default args would force
  # a restart that can hang killing the stale server).
  bazel --batch version >/dev/null 2>&1 || true
  keytool="$(find "$HOME/.cache/bazel" "$HOME/.cache/bazelisk" -type f -name keytool 2>/dev/null | head -n1)"
fi
[ -n "$keytool" ] || {
  echo "keytool not found (no system JDK and none in Bazel's install base); cannot build truststore" >&2
  exit 1
}
echo "using keytool: $keytool"

# Split the bundle into individual PEMs and import each as a trust anchor.
tmp="$(mktemp -d)"
csplit -sz -f "$tmp/c" -b '%02d.pem' "$bundle" '/-----BEGIN CERTIFICATE-----/' '{*}'
rm -f "$store"
n=0
for f in "$tmp"/c*.pem; do
  "$keytool" -importcert -noprompt -storepass "$pass" -storetype PKCS12 \
    -keystore "$store" -alias "egress-$(basename "$f" .pem)" -file "$f"
  n=$((n + 1))
done
[ "$n" -gt 0 ] || {
  echo "no certificates found in $bundle" >&2
  exit 1
}

# Machine-local Bazel config for the runner: the egress-CA truststore (so the
# server JVM trusts the bumped TLS) + the on-disk cache path. Bazel reads
# ~/.bazelrc automatically; nothing checked in.
cat >"$HOME/.bazelrc" <<RC
startup --host_jvm_args=-Djavax.net.ssl.trustStore=$store
startup --host_jvm_args=-Djavax.net.ssl.trustStorePassword=$pass
startup --host_jvm_args=-Djavax.net.ssl.trustStoreType=PKCS12
build --disk_cache=~/.cache/bazel-disk
RC

echo "Imported $n egress CA cert(s) into $store; wrote ~/.bazelrc"
