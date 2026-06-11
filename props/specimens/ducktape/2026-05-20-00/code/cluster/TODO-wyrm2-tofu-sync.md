# wyrm2 tofu state sync

Remaining drift after 2026-04-06 session. All disk changes are done,
state is cleanly imported. These are safe non-disk changes.

## Pending `tofu apply -target=module.wyrm2`

- `agent.timeout`: 15m → 2m (cosmetic, Proxmox-side only)
- `initialization`: remove stale cloud-init drive (old popvm SSH key, DHCP — unused by NixOS)

Neither restarts the VM.

## How to apply

```bash
cd cluster/terraform/main

# Port-forward if not on a k8s worker
kubectl port-forward -n tofu-state svc/tofu-state-db-rw 15432:5432 &

# Creds (use kubectl for PG password since SOPS can't decrypt cluster/k8s/ from atlas)
export SOPS_AGE_KEY=$(ssh-to-age -private-key < ~/.ssh/id_ed25519)
export PG_CONN_STR="postgres://tfstate:$(kubectl get secret -n tofu-state tofu-state-db-credentials -o jsonpath='{.data.password}' | base64 -d)@localhost:15432/tfstate?sslmode=disable"
export PROXMOX_VE_API_TOKEN=$(sops -d --extract '["proxmox_ve_api_token"]' ../../../secrets/shared/cluster-tokens.yaml)
export TF_VAR_hcloud_token=$(sops -d --extract '["hcloud_token"]' ../../../secrets/shared/cluster-tokens.yaml)
export KUBECONFIG=~/.kube/config

tofu plan -target=module.wyrm2
tofu apply -target=module.wyrm2
```

Delete this file after applying.
