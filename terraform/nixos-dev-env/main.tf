# NixOS Dev Environment Infrastructure
# Proxmox infrastructure + dev workstation VMs using the shared nixos-vm module

locals {
  # Proxmox configuration
  proxmox_host     = "root@${var.proxmox_host}"
  proxmox_endpoint = "https://${var.proxmox_api_host}/"
  proxmox_insecure = true # Accept self-signed certs

  # User and pool
  proxmox_user_base  = var.proxmox_username != "" ? var.proxmox_username : var.username
  pool_name_computed = var.pool_name != "" ? var.pool_name : "pool-${local.proxmox_user_base}"
  proxmox_username   = "${local.proxmox_user_base}@pve"

  # VM admin privileges for the pool
  vm_admin_privs = "Pool.Allocate,Sys.Audit,VM.Allocate,VM.Audit,VM.Clone,VM.Config.CDROM,VM.Config.CPU,VM.Config.Cloudinit,VM.Config.Disk,VM.Config.HWType,VM.Config.Memory,VM.Config.Network,VM.Config.Options,VM.Console,VM.Migrate,VM.PowerMgmt,VM.Snapshot,VM.Snapshot.Rollback"

  # SSH key handling - try common key types in order of preference
  ssh_key_candidates = [
    pathexpand("~/.ssh/id_ed25519.pub"),
    pathexpand("~/.ssh/id_ecdsa.pub"),
    pathexpand("~/.ssh/id_rsa.pub")
  ]
  ssh_key_path = var.ssh_public_key != "" ? "" : (
    fileexists(local.ssh_key_candidates[0]) ? local.ssh_key_candidates[0] :
    fileexists(local.ssh_key_candidates[1]) ? local.ssh_key_candidates[1] :
    fileexists(local.ssh_key_candidates[2]) ? local.ssh_key_candidates[2] :
    ""
  )
  ssh_public_key = var.ssh_public_key != "" ? var.ssh_public_key : (
    local.ssh_key_path != "" ? trimspace(file(local.ssh_key_path)) : ""
  )

  # NixOS image build inputs
  nix_dir_hash = sha1(join("", [for f in sort(fileset("${path.module}/../../nix", "**/*.nix")) : filesha1("${path.module}/../../nix/${f}")]))
  repo_root    = "${path.module}/../.."
}

# =============================================================================
# VALIDATION CHECKS
# =============================================================================

check "ssh_key_required" {
  assert {
    condition     = local.ssh_public_key != ""
    error_message = <<-EOT
      No SSH public key found!
      Tried: ${join(", ", local.ssh_key_candidates)}

      Fix by either:
      1. Creating an SSH key: ssh-keygen -t ed25519 -C "your_email@example.com"
      2. Providing key via variable: terraform apply -var="ssh_public_key=$(cat ~/.ssh/id_ed25519.pub)"
    EOT
  }
}

# Check if nix/ tree has uncommitted changes
# (nix/ config is baked into the qcow2 image built locally)
data "external" "git_status" {
  program = ["bash", "-c", <<-EOT
    cd "${path.module}/../.."
    dirty="false"

    if ! git diff --quiet HEAD -- nix/ 2>/dev/null || [ -n "$(git status --porcelain -- nix/ 2>/dev/null)" ]; then
      dirty="true"
    fi

    printf '{"dirty":"%s"}' "$dirty"
  EOT
  ]
}

check "git_clean" {
  assert {
    condition     = data.external.git_status.result.dirty == "false"
    error_message = <<-EOT
      WARNING: nix/ tree has uncommitted changes!
      The VM image is built from committed nix/ config. Uncommitted changes
      will not be included in the image. Commit your changes first.
    EOT
  }
}

# =============================================================================
# PROXMOX USER/TOKEN PROVISIONING
# =============================================================================

data "external" "terraform_user" {
  program = ["bash", "-c", <<-EOT
    ssh ${local.proxmox_host} '
      pveum user add terraform@pve --comment "Terraform automation (ephemeral)" 2>/dev/null || true
      pveum role add TerraformAdmin -privs "Datastore.Allocate,Datastore.AllocateSpace,Datastore.AllocateTemplate,Datastore.Audit,Pool.Allocate,Pool.Audit,SDN.Use,Sys.Audit,Sys.Console,Sys.Modify,VM.Allocate,VM.Audit,VM.Clone,VM.Config.CDROM,VM.Config.CPU,VM.Config.Cloudinit,VM.Config.Disk,VM.Config.HWType,VM.Config.Memory,VM.Config.Network,VM.Config.Options,VM.Console,VM.Migrate,VM.PowerMgmt,User.Modify,Permissions.Modify" 2>/dev/null || \
      pveum role modify TerraformAdmin -privs "Datastore.Allocate,Datastore.AllocateSpace,Datastore.AllocateTemplate,Datastore.Audit,Pool.Allocate,Pool.Audit,SDN.Use,Sys.Audit,Sys.Console,Sys.Modify,VM.Allocate,VM.Audit,VM.Clone,VM.Config.CDROM,VM.Config.CPU,VM.Config.Cloudinit,VM.Config.Disk,VM.Config.HWType,VM.Config.Memory,VM.Config.Network,VM.Config.Options,VM.Console,VM.Migrate,VM.PowerMgmt,User.Modify,Permissions.Modify"
      pveum aclmod / -user terraform@pve -role TerraformAdmin
    '
    printf '{"success":"true"}'
  EOT
  ]
}

data "external" "terraform_token" {
  program = ["bash", "-c", <<-EOT
    token_json=$(ssh ${local.proxmox_host} '
      pveum user token delete terraform@pve terraform 2>/dev/null || true
      pveum user token add terraform@pve terraform --privsep 0 --output-format json
    ')
    token_value=$(echo "$token_json" | jq -r '.value')
    token="terraform@pve!terraform=$token_value"
    printf '{"token":"%s"}' "$token"
  EOT
  ]
  depends_on = [data.external.terraform_user]
}

data "external" "pool_user" {
  program = ["bash", "-c", <<-EOT
    ssh ${local.proxmox_host} '
      pveum user add ${local.proxmox_username} --comment "${var.user_comment}" 2>/dev/null || true
      pveum role add VMAdmin-${local.proxmox_user_base} -privs "${local.vm_admin_privs}" 2>/dev/null || \
      pveum role modify VMAdmin-${local.proxmox_user_base} -privs "${local.vm_admin_privs}"
    '
    printf '{"success":"true"}'
  EOT
  ]
  depends_on = [data.external.terraform_user]
}

data "external" "user_token" {
  program = ["bash", "-c", <<-EOT
    token_json=$(ssh ${local.proxmox_host} '
      pveum user token delete ${local.proxmox_username} api 2>/dev/null || true
      pveum user token add ${local.proxmox_username} api --privsep 0 --output-format json
    ')
    token_value=$(echo "$token_json" | jq -r '.value')
    token="${local.proxmox_username}!api=$token_value"
    printf '{"token":"%s"}' "$token"
  EOT
  ]
  depends_on = [data.external.pool_user]
}

# =============================================================================
# PROXMOX PROVIDERS
# =============================================================================

provider "proxmox" {
  alias     = "admin"
  endpoint  = local.proxmox_endpoint
  username  = "terraform@pve"
  api_token = data.external.terraform_token.result.token
  insecure  = local.proxmox_insecure
}

provider "proxmox" {
  alias     = "user"
  endpoint  = local.proxmox_endpoint
  username  = local.proxmox_username
  api_token = data.external.user_token.result.token
  insecure  = local.proxmox_insecure

  ssh {
    agent    = true
    username = "root"
    node {
      name    = var.proxmox_node_name
      address = var.proxmox_host
    }
  }
}

provider "proxmox" {
  endpoint  = local.proxmox_endpoint
  username  = "terraform@pve"
  api_token = data.external.terraform_token.result.token
  insecure  = local.proxmox_insecure
}

# =============================================================================
# SHARED INFRASTRUCTURE
# =============================================================================

resource "proxmox_virtual_environment_pool" "user_pool" {
  comment = "Resource pool for ${local.proxmox_user_base}"
  pool_id = local.pool_name_computed
}

resource "proxmox_virtual_environment_acl" "pool_admin" {
  path      = "/pool/${proxmox_virtual_environment_pool.user_pool.pool_id}"
  role_id   = "VMAdmin-${local.proxmox_user_base}"
  user_id   = local.proxmox_username
  propagate = true

  depends_on = [data.external.pool_user]
}

resource "proxmox_virtual_environment_acl" "storage_access" {
  path    = "/storage/${var.storage}"
  role_id = "PVEDatastoreUser"
  user_id = local.proxmox_username
}

resource "proxmox_virtual_environment_acl" "storage_access_local" {
  path    = "/storage/local"
  role_id = "PVEDatastoreAdmin"
  user_id = local.proxmox_username
}

resource "proxmox_virtual_environment_acl" "sdn_access" {
  path      = "/sdn"
  role_id   = "PVESDNUser"
  user_id   = local.proxmox_username
  propagate = true
}

# Per-host NixOS qcow2 images (built via nix, uploaded to Proxmox)
# Uses system.build.images.qemu-efi (nixos-generators upstreamed in nixpkgs 25.05+)
module "wyrm2_image" {
  source       = "../modules/nixos-image"
  flake_target = "wyrm2"
  proxmox_host = var.proxmox_host
  repo_root    = local.repo_root
  nix_dir_hash = local.nix_dir_hash
}

# Cleanup on destroy
resource "null_resource" "cleanup" {
  triggers = {
    username     = local.proxmox_username
    proxmox_host = local.proxmox_host
    role_name    = "VMAdmin-${local.proxmox_user_base}"
  }

  provisioner "local-exec" {
    when    = destroy
    command = <<-EOT
      echo "Cleaning up Proxmox users and roles"
      ssh ${self.triggers.proxmox_host} '
        pveum user token delete ${self.triggers.username} api 2>/dev/null || true
        pveum user token delete terraform@pve terraform 2>/dev/null || true
        pveum user delete ${self.triggers.username} 2>/dev/null || true
        pveum user delete terraform@pve 2>/dev/null || true
        if [ "$(pveum aclmod / -role ${self.triggers.role_name} 2>/dev/null | wc -l)" -eq 0 ]; then
          pveum role delete ${self.triggers.role_name} 2>/dev/null || true
        fi
        pveum role delete TerraformAdmin 2>/dev/null || true
        echo "Cleanup completed"
      ' || true
    EOT
  }
}

# =============================================================================
# VM INSTANCES
# =============================================================================

# Wyrm2 - NixOS dev workstation (pre-built image, no cloud-init bootstrap)
module "wyrm2" {
  source = "../modules/nixos-vm"
  providers = {
    proxmox = proxmox.user
  }

  vm_name          = "wyrm2"
  vm_id            = 110
  username         = var.username
  vcpus            = 8
  memory_mb        = 16384
  disk_size_gb     = 100
  auto_start       = true
  image_import_path = module.wyrm2_image.import_path

  proxmox_node_name = var.proxmox_node_name
  storage           = var.storage
  network_bridge    = var.network_bridge
  pool_id           = proxmox_virtual_environment_pool.user_pool.pool_id
  ssh_public_key    = local.ssh_public_key

  depends_on = [
    proxmox_virtual_environment_acl.pool_admin,
    proxmox_virtual_environment_acl.storage_access,
    proxmox_virtual_environment_acl.storage_access_local,
    module.wyrm2_image,
    null_resource.cleanup
  ]
}
