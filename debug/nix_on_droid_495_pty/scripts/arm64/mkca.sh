#!/bin/bash
# Build the CA material the guest needs to trust this container's egress proxy.
#
# Two distinct trust stores have to be fed, because two different stacks make
# the network calls:
#   - Android's system store (<hash>.0 files, subject_hash_old naming) is what
#     the com.termux.nix APK's Java HTTP client uses to fetch the bootstrap.
#   - A plain PEM bundle is what nix-on-droid's own curl/Nix use inside the
#     proot, via NIX_SSL_CERT_FILE / SSL_CERT_FILE.
set -euo pipefail
D=/tmp/claude-0/-home-user-ducktape/17570302-6f29-5e29-9145-7d878c0711db/scratchpad
mkdir -p "$D/ca"
H=$(openssl x509 -inform PEM -subject_hash_old -in /root/.ccr/agent-proxy-ca.crt -noout)
echo "android cacert filename: $H.0"
cp /root/.ccr/agent-proxy-ca.crt "$D/ca/$H.0"
openssl x509 -inform PEM -text -fingerprint -noout -in /root/.ccr/agent-proxy-ca.crt >>"$D/ca/$H.0"
cp /root/.ccr/ca-bundle.crt "$D/ca/ca-bundle.crt"
echo "$H" >"$D/ca/hash.txt"
ls -la "$D/ca/"
