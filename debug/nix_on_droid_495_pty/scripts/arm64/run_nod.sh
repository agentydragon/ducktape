#!/bin/bash
# Run a command inside nix-on-droid's proot with the proxy and CA wired in.
#
# bin/login execs proot and then `exec /usr/bin/env "$@"` when given arguments
# (modules/environment/login/login-inner.nix), so environment set here reaches
# Nix inside the proot. Nix and curl use their own CA bundle, not Android's, so
# they are pointed at the proxy bundle explicitly; the file lives under the
# app's own directory, which the proot binds at /etc.
#
# USE_FLAKE short-circuits login-inner's interactive "set it up with flakes?"
# prompt on the very first run.
set -uo pipefail
D=/tmp/claude-0/-home-user-ducktape/17570302-6f29-5e29-9145-7d878c0711db/scratchpad
ADB="$D/sdk/platform-tools/adb"
SER=emulator-5554
APPUID=${APPUID:?set APPUID to the uid of com.termux.nix}
PROXY=http://10.0.2.2:39587
U=/data/data/com.termux.nix/files/usr

"$ADB" -s "$SER" shell "su $APPUID env \
  http_proxy=$PROXY https_proxy=$PROXY \
  NIX_SSL_CERT_FILE=/etc/proxy-ca-bundle.crt \
  SSL_CERT_FILE=/etc/proxy-ca-bundle.crt \
  CURL_CA_BUNDLE=/etc/proxy-ca-bundle.crt \
  USE_FLAKE=${USE_FLAKE:-1} \
  $U/bin/login $*"
