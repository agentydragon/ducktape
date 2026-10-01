#!/bin/bash
# Verify the egress proxy works for a client that is told about it only
# explicitly -- no inherited HTTPS_PROXY, no inherited CA env. This is exactly
# the shape of the request the guest will make (CONNECT to 127.0.0.1:39587,
# verifying against the proxy CA bundle), so a failure here would be a host
# problem rather than a guest one.
set -u
env -u HTTPS_PROXY -u https_proxy -u CURL_CA_BUNDLE -u SSL_CERT_FILE -u REQUESTS_CA_BUNDLE \
  curl -sS -o /dev/null -m 60 \
  --proxy http://127.0.0.1:39587 \
  --cacert /root/.ccr/ca-bundle.crt \
  -w 'cache.nixos.org status=%{http_code}\n' \
  https://cache.nixos.org/nix-cache-info
env -u HTTPS_PROXY -u https_proxy -u CURL_CA_BUNDLE -u SSL_CERT_FILE -u REQUESTS_CA_BUNDLE \
  curl -sS -o /dev/null -m 60 \
  --proxy http://127.0.0.1:39587 \
  --cacert /root/.ccr/ca-bundle.crt \
  -w 'nix-on-droid.unboiled.info status=%{http_code}\n' \
  https://nix-on-droid.unboiled.info/bootstrap-release-24.05/
