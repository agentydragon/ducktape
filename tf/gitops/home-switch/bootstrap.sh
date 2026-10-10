#!/usr/bin/env bash
# Prepares the home switch for tf/gitops/home-switch, once per factory reset: puts the cluster-CA
# certificate on api-ssl, which the Terraform verifies, and sets a fresh random password
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
  kubectl get secret --namespace monitoring home-switch-tls --output "jsonpath={.data.${key//./\\.}}" \
    | base64 --decode >"$tmp/home-switch-tls.${key#tls.}"
done
# The name main.tf's routeros_system_certificate.tls gives this certificate, so its first apply
# adopts it rather than installing a second one.
cert="home-switch-$(sha1sum <"$tmp/home-switch-tls.crt" | cut -c1-8)"

# Hex, so it needs no quoting inside the RouterOS script.
password=$(openssl rand -hex 24)

scp -q "${ssh_opts[@]}" "$tmp/home-switch-tls.crt" "$tmp/home-switch-tls.key" "$admin@$switch:"
# RouterOS runs each line of an SSH command on its own, so every block stays on one line. It also
# exits 0 when a command fails, hence the read-back check below. Older certificates for the switch
# go (main.tf, routeros_system_certificate.tls, says why).
ssh "${ssh_opts[@]}" "$admin@$switch" "
:if ([:len [/certificate find name=$cert]] = 0) do={ /certificate import file-name=home-switch-tls.crt name=$cert passphrase=\"\"; /certificate import file-name=home-switch-tls.key name=$cert passphrase=\"\" }
/file remove [find name~\"home-switch-tls\"]
/ip service set api-ssl certificate=$cert address=$lan_cidr disabled=no
/ip service set www-ssl certificate=$cert
/certificate remove [find where name~\"^home-switch-\" and name!=\"$cert\"]
:if ([:len [/user find name=tofu]] = 0) do={ /user add name=tofu group=full address=$lan_cidr password=$password comment=\"tf/gitops/home-switch\" } else={ /user set tofu group=full address=$lan_cidr password=$password }
"
ssh "${ssh_opts[@]}" "$admin@$switch" \
  ":if ([/ip service get api-ssl certificate] = \"$cert\" && ![/ip service get api-ssl disabled] && [/user get tofu group] = \"full\" && [/certificate get $cert private-key]) do={ :put \"bootstrap-ok\" }" \
  | grep bootstrap-ok >/dev/null || {
  echo "The switch did not end up with api-ssl on $cert and a full-access tofu user." >&2
  exit 1
}

# Only now, so a failed run leaves the committed password alone.
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
  echo "Encryption failed; the switch has a password nothing records, so re-run once sops works." >&2
  exit 1
}

echo "The switch has the new tofu password. Commit and merge ${password_file#"$(git rev-parse --show-toplevel)/"}."
