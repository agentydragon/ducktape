# PERSISTENT AUTH
# Persistent authentication credentials that survive VM lifecycle.
# Includes: CSI tokens, Nebula PKI,
# Nix cache signing key, Attic JWT token, SOPS age keypair.

# PROXMOX USERS, ROLES, AND TOKENS

# TODO: Consider whether terraform@pve is still needed now that root@pam!tofu exists.
# Infrastructure and k8s-worker-proxmox could read the root token from the keyring via
# their own .envrc instead. Keeping for now for least-privilege (TerraformAdmin is
# narrower than root).
#
# The kubernetes-csi@pve user/token was removed 2026-07-16 with the Proxmox CSI driver
# (see cluster/docs/lessons_learned/2026_07_16_disable_proxmox_csi.md).
locals {
  # Persistent Proxmox users - survive VM lifecycle
  pve_persistent_users = {
    terraform = {
      name    = "terraform@pve"
      comment = "Terraform automation user (persistent)"
      role    = "TerraformAdmin"
      privs = [
        "Datastore.Allocate",
        "Datastore.AllocateSpace",
        "Datastore.AllocateTemplate",
        "Datastore.Audit",
        "Mapping.Modify",
        "Mapping.Use",
        "Permissions.Modify",
        "Pool.Allocate",
        "SDN.Use",
        "Sys.Audit",
        "Sys.Console",
        "Sys.Modify",
        "User.Modify",
        "VM.Allocate",
        "VM.Audit",
        "VM.Clone",
        "VM.Config.CDROM",
        "VM.Config.CPU",
        "VM.Config.Cloudinit",
        "VM.Config.Disk",
        "VM.Config.HWType",
        "VM.Config.Memory",
        "VM.Config.Network",
        "VM.Config.Options",
        "VM.Console",
        "VM.GuestAgent.Audit",
        "VM.Migrate",
        "VM.PowerMgmt",
      ]
      token = "terraform-token"
    }
  }
}

resource "proxmox_virtual_environment_role" "persistent" {
  for_each   = local.pve_persistent_users
  role_id    = each.value.role
  privileges = each.value.privs

  lifecycle { prevent_destroy = true }
}

resource "proxmox_virtual_environment_user" "persistent" {
  for_each = local.pve_persistent_users
  user_id  = each.value.name
  comment  = each.value.comment
  acl {
    path      = "/"
    role_id   = proxmox_virtual_environment_role.persistent[each.key].role_id
    propagate = true
  }

  lifecycle { prevent_destroy = true }
}

resource "proxmox_virtual_environment_user_token" "persistent" {
  for_each              = local.pve_persistent_users
  user_id               = proxmox_virtual_environment_user.persistent[each.key].user_id
  token_name            = each.value.token
  privileges_separation = false
  comment               = "Managed by OpenTofu"

  lifecycle { prevent_destroy = true }
}
# NEBULA MESH PKI — persisted CA + per-node certificate material

locals {
  # Tofu-managed Talos nodes — derived from the mesh roster
  # (../../../nebula-mesh.json). The already-issued certificates and keys live
  # in secrets/nebula/, and Tofu embeds them in machine config patches
  # (nebula.tf). To add a new Talos node, persist its identity first, then edit
  # the roster — see cluster/docs/mesh_membership.md.
  #
  # Cert names use FQDN under nebula.allegedly.works so that systemd-resolved
  # can route queries via ~nebula.allegedly.works without +DefaultRoute (which
  # breaks public DNS when cluster nodes are unreachable).
  #
  # Every Nebula node uses secrets/nebula/. Private keys are SOPS-encrypted;
  # public certificates are plaintext. See cluster/docs/secrets.md.
  talos_nebula_nodes = {
    for name, h in local.nebula_hosts :
    name => {}
    if startswith(try(h.managed_by, ""), "tofu-")
  }
}

# Nebula CA public certificate.
data "local_file" "nebula_ca_crt" {
  filename = "${path.module}/../../../secrets/nebula/ca.crt"
}

# Per-node identities are durable input material, not local-exec output. This
# keeps a fresh Terraform workstation from silently rotating a live node key.
data "local_file" "nebula_node_crt" {
  for_each = local.talos_nebula_nodes
  filename = "${path.module}/../../../secrets/nebula/${each.key}.crt"
}

data "sops_file" "nebula_node_key" {
  for_each    = local.talos_nebula_nodes
  source_file = "${path.module}/../../../secrets/nebula/${each.key}.sops.key"
  input_type  = "raw"
}

# SOPS AGE KEYPAIR — cluster k8s secrets
# Keypair stored in secrets/shared/cluster-secrets-age.yaml (SOPS-encrypted to admin +
# user keys). Public key in .sops.yaml (&cluster-secrets anchor).
# Tofu decrypts via sops provider and deploys the private key to flux-system.

data "sops_file" "cluster_secrets_age" {
  source_file = "${path.module}/../../../secrets/shared/cluster-secrets-age.yaml"
}

resource "kubernetes_namespace" "flux_system" {
  metadata {
    name = "flux-system"
  }

  lifecycle {
    ignore_changes = [metadata[0].annotations, metadata[0].labels]
  }

  depends_on = [
    local_file.kubeconfig,
    null_resource.wait_for_k8s_api,
    null_resource.cilium_bootstrap,
  ]
}

resource "kubernetes_secret" "sops_age_cluster_secrets" {
  metadata {
    name      = "sops-age-cluster-secrets"
    namespace = "flux-system"
  }

  data = {
    "age.agekey" = data.sops_file.cluster_secrets_age.data["age_secret_key"]
  }

  type = "Opaque"

  depends_on = [kubernetes_namespace.flux_system]
}
