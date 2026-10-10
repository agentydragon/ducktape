#!/usr/bin/env bash
# Prepares the home switch for tf/gitops/home-switch, once per factory reset: a self-signed
# certificate on api-ssl, and the full-access `tofu` user with the ESO-minted password. Safe to
# re-run. Needs kubectl access to the cluster and SSH to the switch as an existing admin.
#
# usage: bootstrap.sh [admin-user] [switch-address]
set -euo pipefail

admin="${1:-admin}"
switch="${2:-192.168.1.100}"
lan_cidr="192.168.1.0/24" # keep in sync with local.lan_cidr in main.tf

password=$(kubectl get secret --namespace monitoring home-switch-tofu --output jsonpath='{.data.password}' | base64 --decode)

ssh "$admin@$switch" "
:if ([:len [/certificate find name=api]] = 0) do={
  /certificate add name=api common-name=CRS310 subject-alt-name=IP:$switch days-valid=3650 key-usage=digital-signature,key-encipherment,tls-server
  /certificate sign api
}
/ip service set api-ssl certificate=api address=$lan_cidr disabled=no
:if ([:len [/user find name=tofu]] = 0) do={
  /user add name=tofu group=full address=$lan_cidr password=\"$password\" comment=\"tf/gitops/home-switch\"
} else={
  /user set tofu group=full address=$lan_cidr password=\"$password\"
}
"
