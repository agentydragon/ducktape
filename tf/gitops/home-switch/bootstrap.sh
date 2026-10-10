#!/usr/bin/env bash
# Prepares the home switch for tf/gitops/home-switch, once per factory reset: puts the cluster-CA
# bootstrap certificate on api-ssl, which the Terraform verifies, and sets a fresh random password
# on the full-access `tofu` user, creating it if needed. The password goes SOPS-encrypted into
# cluster/k8s/home-switch/tofu-password.sops.yaml for you to commit; re-running rotates it.
#
# Run from the repository (direnv loaded) on a home-LAN host with kubectl access. SSH asks for the
# switch admin's password once.
#
# usage: bootstrap.sh [admin-user] [switch-address]
set -euo pipefail

admin="${1:-admin}"
switch="${2:-192.168.1.100}"
lan_cidr="192.168.1.0/24" # keep in sync with _LAN_CIDR in cluster/cdk8s/monitoring/home_switch.py
# Secret name: keep in sync with data.kubernetes_secret_v1.tofu in main.tf.
password_file="$(git rev-parse --show-toplevel)/cluster/k8s/home-switch/tofu-password.sops.yaml"

tmp=$(mktemp -d)
ssh_opts=(-o ControlMaster=auto -o "ControlPath=$tmp/ssh" -o ControlPersist=60)
trap 'ssh "${ssh_opts[@]}" -O exit "$admin@$switch" 2>/dev/null || true; rm -rf "$tmp"' EXIT

for key in tls.crt tls.key; do
  kubectl get secret --namespace monitoring home-switch-bootstrap-tls --output "jsonpath={.data.${key//./\\.}}" \
    | base64 --decode >"$tmp/home-switch-bootstrap.${key#tls.}"
done

# Hex, so it needs no quoting inside the RouterOS script.
password=$(openssl rand -hex 24)
cat >"$password_file" <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: home-switch-tofu-password
  namespace: monitoring
stringData:
  password: $password
EOF
sops --encrypt --in-place "$password_file" || {
  git checkout -- "$password_file"
  exit 1
}

scp -q "${ssh_opts[@]}" "$tmp/home-switch-bootstrap.crt" "$tmp/home-switch-bootstrap.key" "$admin@$switch:"
ssh "${ssh_opts[@]}" "$admin@$switch" "
:if ([:len [/certificate find name=bootstrap]] = 0) do={
  /certificate import file-name=home-switch-bootstrap.crt name=bootstrap passphrase=\"\"
  /certificate import file-name=home-switch-bootstrap.key name=bootstrap passphrase=\"\"
}
/file remove [find name~\"home-switch-bootstrap\"]
/ip service set api-ssl certificate=bootstrap address=$lan_cidr disabled=no
:if ([:len [/user find name=tofu]] = 0) do={
  /user add name=tofu group=full address=$lan_cidr password=$password comment=\"tf/gitops/home-switch\"
} else={
  /user set tofu group=full address=$lan_cidr password=$password
}
"

echo "The switch has the new tofu password. Commit and merge ${password_file#"$(git rev-parse --show-toplevel)/"}."
