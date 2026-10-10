#!/usr/bin/env bash
# Prepares the home switch for tf/gitops/home-switch, once per factory reset: a cluster-CA
# certificate on api-ssl, which the Terraform verifies, and the full-access `tofu` user with the
# ESO-minted password. Safe to re-run. Needs kubectl access to the cluster and SSH to the switch as
# an existing admin.
#
# usage: bootstrap.sh [admin-user] [switch-address]
set -euo pipefail

admin="${1:-admin}"
switch="${2:-192.168.1.100}"
lan_cidr="192.168.1.0/24" # keep in sync with local.lan_cidr in main.tf

secret() {
  kubectl get secret --namespace monitoring "$1" --output jsonpath="{.data.${2//./\\.}}" | base64 --decode
}

password=$(secret home-switch-tofu password)

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
secret home-switch-bootstrap-tls tls.crt >"$tmp/home-switch-bootstrap.crt"
secret home-switch-bootstrap-tls tls.key >"$tmp/home-switch-bootstrap.key"
scp -q "$tmp/home-switch-bootstrap.crt" "$tmp/home-switch-bootstrap.key" "$admin@$switch:"

ssh "$admin@$switch" "
:if ([:len [/certificate find name=bootstrap]] = 0) do={
  /certificate import file-name=home-switch-bootstrap.crt name=bootstrap passphrase=\"\"
  /certificate import file-name=home-switch-bootstrap.key name=bootstrap passphrase=\"\"
}
/file remove [find name~\"home-switch-bootstrap\"]
/ip service set api-ssl certificate=bootstrap address=$lan_cidr disabled=no
:if ([:len [/user find name=tofu]] = 0) do={
  /user add name=tofu group=full address=$lan_cidr password=\"$password\" comment=\"tf/gitops/home-switch\"
} else={
  /user set tofu group=full address=$lan_cidr password=\"$password\"
}
"
