#!/usr/bin/env bash
set -euo pipefail
umask 077

proxy_ca=$1
system_bundle=$2
proxy_state=$3
proxy_nss=${4:-}

if [[ ! -f $proxy_ca ]]; then
  echo "github-api-proxy: pinned public CA is unavailable" >&2
  exit 1
fi
install -d -m 0700 "$proxy_state"
# Nix-store certificate mtimes do not change with a new CA pin.
proxy_bundle_tmp=$(mktemp "$proxy_state/ca-bundle.XXXXXX")
trap 'rm -f -- "$proxy_bundle_tmp"' EXIT
cat "$system_bundle" "$proxy_ca" >"$proxy_bundle_tmp"
mv -f -- "$proxy_bundle_tmp" "$proxy_state/ca-bundle.pem"

if [[ -n $proxy_nss ]]; then
  install -d -m 0700 "$proxy_nss"
  if [[ ! -f $proxy_nss/cert9.db ]]; then
    certutil -N --empty-password -d "sql:$proxy_nss"
  fi
  # -A rejects a changed certificate under an existing nickname.
  if certutil -L -d "sql:$proxy_nss" -n ducktape-github-api-proxy >/dev/null 2>&1; then
    certutil -D -d "sql:$proxy_nss" -n ducktape-github-api-proxy
  fi
  certutil -A -d "sql:$proxy_nss" -n ducktape-github-api-proxy -t C,, -i "$proxy_ca"
fi
