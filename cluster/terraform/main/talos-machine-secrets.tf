# Talos Machine Secrets — persistent across cluster lifecycles.
#
# On first bootstrap, this generates fresh secrets (CA keypairs, bootstrap token,
# etcd certs, etc.). The resource has prevent_destroy and lives in persistent-auth
# so it survives teardown/rebuild cycles.
#
# The bootstrap script exports machine_secrets to SOPS as the durable source of
# truth (secrets/talos-machine-secrets.sops.yaml), then derives:
#   - secrets/k8s-ca.crt (plaintext PEM, for NixOS workers)
#   - secrets/k8s-worker.yaml (SOPS-encrypted bootstrap token, for NixOS workers)
#
# If tofu state is lost, recover with:
#   sops -d secrets/talos-machine-secrets.sops.yaml > /tmp/ms.yaml
#   tofu import talos_machine_secrets.cluster /tmp/ms.yaml && rm /tmp/ms.yaml

resource "talos_machine_secrets" "cluster" {
  talos_version = var.talos_version

  lifecycle {
    prevent_destroy = true
  }
}
