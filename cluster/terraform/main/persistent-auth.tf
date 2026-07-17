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
# NEBULA MESH PKI — CA + per-node certificates

locals {
  nebula_cert_dir = "${path.module}/nebula-certs"

  # Tofu-managed Talos nodes — derived from the mesh roster
  # (../../../nebula-mesh.json). Tofu issues certs and embeds them in machine
  # config patches (nebula.tf). To add a new Talos node, edit the roster — see
  # cluster/docs/mesh_membership.md.
  #
  # Cert names use FQDN under nebula.allegedly.works so that systemd-resolved
  # can route queries via ~nebula.allegedly.works without +DefaultRoute (which
  # breaks public DNS when cluster nodes are unreachable).
  #
  # Non-tofu nodes (atlas, wyrm2, rugged, iguana, pixel6) have certs in
  # secrets/nebula/. See docs/secrets.md "Nebula Certs for Non-Talos Nodes".
  talos_nebula_nodes = {
    for name, h in local.nebula_hosts :
    "${name}.nebula.allegedly.works" => {
      ip     = "${h.nebula_ip}/16"
      groups = join(",", try(h.cert_groups, []))
    }
    if startswith(try(h.managed_by, ""), "tofu-")
  }
}

# Nebula CA — plaintext cert + SOPS binary key (secrets/nebula/).
data "local_file" "nebula_ca_crt" {
  filename = "${path.module}/../../../secrets/nebula/ca.crt"
}

data "sops_file" "nebula_ca_key" {
  source_file = "${path.module}/../../../secrets/nebula/ca.sops.key"
  input_type  = "raw"
}

resource "local_sensitive_file" "nebula_ca_key" {
  content  = data.sops_file.nebula_ca_key.raw
  filename = "${local.nebula_cert_dir}/ca.key"
}

resource "local_file" "nebula_ca_crt" {
  content  = data.local_file.nebula_ca_crt.content
  filename = "${local.nebula_cert_dir}/ca.crt"
}

# Generate per-node certs signed by the CA.
resource "null_resource" "nebula_node_cert" {
  for_each = local.talos_nebula_nodes

  triggers = {
    ca_hash = sha256(local_file.nebula_ca_crt.content)
    ip      = each.value.ip
    groups  = each.value.groups
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      nebula-cert sign \
        -ca-crt ${local.nebula_cert_dir}/ca.crt \
        -ca-key ${local.nebula_cert_dir}/ca.key \
        -name "${each.key}" \
        -ip "${each.value.ip}" \
        -groups "${each.value.groups}" \
        -out-crt ${local.nebula_cert_dir}/${each.key}.crt \
        -out-key ${local.nebula_cert_dir}/${each.key}.key
    EOT
  }

  depends_on = [local_file.nebula_ca_crt, local_sensitive_file.nebula_ca_key]
}

data "local_file" "nebula_node_crt" {
  for_each   = local.talos_nebula_nodes
  filename   = "${local.nebula_cert_dir}/${each.key}.crt"
  depends_on = [null_resource.nebula_node_cert]
}

data "local_sensitive_file" "nebula_node_key" {
  for_each   = local.talos_nebula_nodes
  filename   = "${local.nebula_cert_dir}/${each.key}.key"
  depends_on = [null_resource.nebula_node_cert]
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
