# NixOS Dev Environment Infrastructure
# Shared Proxmox infrastructure + VM instances using the nixos-vm module

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
  vm_admin_privs = "VM.Allocate,VM.Audit,VM.Clone,VM.Config.CDROM,VM.Config.CPU,VM.Config.Cloudinit,VM.Config.Disk,VM.Config.HWType,VM.Config.Memory,VM.Config.Network,VM.Config.Options,VM.Console,VM.Migrate,VM.Monitor,VM.PowerMgmt,VM.Snapshot,VM.Snapshot.Rollback"

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

  # Environment variables for VMs
  proxmox_env_vars = {
    PROXMOX_VE_ENDPOINT  = local.proxmox_endpoint
    PROXMOX_VE_USERNAME  = local.proxmox_username
    PROXMOX_VE_API_TOKEN = data.external.user_token.result.token
    PROXMOX_VE_INSECURE  = tostring(local.proxmox_insecure)
    PROXMOX_POOL_ID      = local.pool_name_computed
  }

  llm_api_keys = merge(
    var.openai_api_key != "" ? { OPENAI_API_KEY = var.openai_api_key } : {},
    var.anthropic_api_key != "" ? { ANTHROPIC_API_KEY = var.anthropic_api_key } : {}
  )
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

# Check if ducktape repo has uncommitted changes or unpushed commits
data "external" "git_status" {
  program = ["bash", "-c", <<-EOT
    cd "${path.module}/../.."
    dirty="false"
    unpushed="false"

    if ! git diff --quiet HEAD 2>/dev/null || [ -n "$(git status --porcelain 2>/dev/null)" ]; then
      dirty="true"
    fi

    if [ "$(git rev-parse HEAD 2>/dev/null)" != "$(git rev-parse origin/devel 2>/dev/null)" ]; then
      unpushed="true"
    fi

    printf '{"dirty":"%s","unpushed":"%s"}' "$dirty" "$unpushed"
  EOT
  ]
}

check "git_clean" {
  assert {
    condition     = data.external.git_status.result.dirty == "false"
    error_message = <<-EOT
      WARNING: Ducktape repo has uncommitted changes!
      The VM will fetch home-manager config from GitHub, not your local changes.
      Commit and push your changes first, or your VM config may be outdated.
    EOT
  }
}

check "git_pushed" {
  assert {
    condition     = data.external.git_status.result.unpushed == "false"
    error_message = <<-EOT
      WARNING: Ducktape repo has unpushed commits on devel branch!
      The VM will fetch home-manager config from GitHub, not your local commits.
      Push your changes first: git push origin devel
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
      pveum role add TerraformAdmin -privs "Datastore.Allocate,Datastore.AllocateSpace,Datastore.AllocateTemplate,Datastore.Audit,Pool.Allocate,Pool.Audit,SDN.Use,Sys.Audit,Sys.Console,Sys.Modify,VM.Allocate,VM.Audit,VM.Clone,VM.Config.CDROM,VM.Config.CPU,VM.Config.Cloudinit,VM.Config.Disk,VM.Config.HWType,VM.Config.Memory,VM.Config.Network,VM.Config.Options,VM.Console,VM.Migrate,VM.Monitor,VM.PowerMgmt,User.Modify,Permissions.Modify" 2>/dev/null || \
      pveum role modify TerraformAdmin -privs "Datastore.Allocate,Datastore.AllocateSpace,Datastore.AllocateTemplate,Datastore.Audit,Pool.Allocate,Pool.Audit,SDN.Use,Sys.Audit,Sys.Console,Sys.Modify,VM.Allocate,VM.Audit,VM.Clone,VM.Config.CDROM,VM.Config.CPU,VM.Config.Cloudinit,VM.Config.Disk,VM.Config.HWType,VM.Config.Memory,VM.Config.Network,VM.Config.Options,VM.Console,VM.Migrate,VM.Monitor,VM.PowerMgmt,User.Modify,Permissions.Modify"
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
      pveum role add VMAdmin-${local.proxmox_user_base} -privs "${local.vm_admin_privs}" 2>/dev/null || true
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
  role_id   = "PVEVMAdmin"
  user_id   = local.proxmox_username
  propagate = true
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

# NixOS cloud image (shared by all VMs)
resource "null_resource" "nixos_cloud_image" {
  triggers = {
    cloud_image_config = filemd5("${path.module}/cloud-image.nix")
    proxmox_host       = var.proxmox_host
    storage            = var.storage
  }

  provisioner "local-exec" {
    command     = <<-EOT
      set -e
      echo "Building NixOS qcow2 cloud image..."

      nix run github:nix-community/nixos-generators -- \
        --format qcow-efi \
        --configuration ${path.module}/cloud-image.nix \
        -o nixos-cloud-image

      QCOW2_PATH=$(readlink -f nixos-cloud-image)/nixos.qcow2

      echo "Uploading qcow2 to Proxmox import directory..."
      ssh root@${var.proxmox_host} "mkdir -p /var/lib/vz/import"
      scp "$QCOW2_PATH" "root@${var.proxmox_host}:/var/lib/vz/import/nixos-cloud.qcow2"

      echo "qcow2 image ready for import at local:import/nixos-cloud.qcow2"
    EOT
    working_dir = path.module
  }
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

# Wyrm2 - NixOS dev workstation
module "wyrm2" {
  source = "./modules/nixos-vm"
  providers = {
    proxmox = proxmox.user
  }

  vm_name      = "wyrm2"
  vm_id        = 110
  username     = var.username
  vcpus        = 8
  memory_mb    = 16384
  disk_size_gb = 100
  auto_start   = true

  # NixOS config from flake
  nixos_flake_url = var.nixos_flake_url
  nixos_host      = "wyrm2"

  # Home-manager config from flake
  home_manager_flake_url = var.home_manager_flake_url
  home_manager_host      = var.home_manager_host

  proxmox_node_name = var.proxmox_node_name
  storage           = var.storage
  network_bridge    = var.network_bridge
  pool_id           = proxmox_virtual_environment_pool.user_pool.pool_id
  ssh_public_key    = local.ssh_public_key

  depends_on = [
    proxmox_virtual_environment_acl.pool_admin,
    proxmox_virtual_environment_acl.storage_access,
    proxmox_virtual_environment_acl.storage_access_local,
    null_resource.nixos_cloud_image,
    null_resource.cleanup
  ]
}
