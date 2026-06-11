# NixOS Dev Environment with Ducktape Home-Manager

Unified Terraform configuration that creates a complete isolated NixOS development environment in Proxmox:

- Proxmox user with resource pool (isolated from other VMs)
- NixOS VM with latest channel (unstable, 24.11, or 24.05)
- Ducktape home-manager configuration
- User 'user' (or custom) with no password and auto-login
- GNOME desktop (optional)

## Features

✅ **Isolated Environment**: User can only see/manage VMs in their pool
✅ **Unattended Setup**: Cloud-init handles full NixOS and home-manager installation
✅ **Latest NixOS**: Choose from unstable, 24.11, or 24.05 channels
✅ **Ducktape Integration**: Automatically sets up home-manager with your ducktape config
✅ **Passwordless Access**: SSH keys + auto-login for seamless development
✅ **Least Privilege**: Uses user's own credentials to create the VM
✅ **Proxmox Credentials Baked In**: VM can manage itself and create sibling VMs
✅ **Custom Environment Variables**: Inject arbitrary env vars into the VM

## Prerequisites

- SSH access to Proxmox host as root (password-less via SSH keys)
- Terraform >= 1.0
- `jq` installed locally

## Quick Start

```bash
# 1. Copy example configuration
cp example.tfvars terraform.tfvars

# 2. Edit terraform.tfvars
vim terraform.tfvars
# Set at minimum:
#   username = "yourname"

# 3. (Optional) Export LLM API keys to copy them to VM
export OPENAI_API_KEY="sk-..."
export ANTHROPIC_API_KEY="sk-ant-..."

# 4. Initialize and apply
terraform init
./apply.sh apply  # or: terraform apply

# 5. Wait for cloud-init to complete (~2-3 minutes)
# Monitor via Proxmox console or:
watch terraform output vm_ipv4_addresses

# 5. SSH into your VM
ssh user@<vm-ip>
```

## Architecture

### Two-Phase Provisioning

**Phase 1: Pool & User Creation** (using terraform admin credentials):

- Creates Proxmox user `{username}@pve`
- Creates resource pool `pool-{username}`
- Assigns `PVEVMAdmin` permissions on the pool
- Generates API token for the user

**Phase 2: VM Creation** (using user's credentials):

- Downloads NixOS ISO
- Creates NixOS VM in the user's pool
- Uploads configuration files (NixOS config, home-manager flake)
- Provisions via cloud-init

This ensures least-privilege: the VM is created with the user's own credentials, not admin credentials.

## What Gets Created

### Proxmox Resources

- **User**: `{username}@pve` with API token
- **Pool**: `pool-{username}` (or custom name)
- **Permissions**: PVEVMAdmin on pool, PVEVMUser on storage
- **VM**: NixOS with chosen channel

### NixOS Configuration

- **Nix flakes**: Enabled
- **User**: Passwordless with sudo
- **SSH**: Key-based auth only
- **Desktop**: GNOME with auto-login (if GUI enabled)
- **Screen lock**: Disabled
- **Home-manager**: Configured with ducktape repository

### Files Structure

```
/etc/nixos/
  ├── configuration.nix    # System config (from Terraform template)
  └── hardware-configuration.nix

/home/{username}/.config/home-manager/
  └── flake.nix            # Points to ducktape repo
```

## Baked-In Environment Variables

### Proxmox Credentials (automatic)

The VM automatically receives these environment variables (machine-wide via NixOS):

```bash
PROXMOX_VE_ENDPOINT=https://atlas.agentydragon.com:8006
PROXMOX_VE_USERNAME=user@pve
PROXMOX_VE_API_TOKEN=user@pve!api=<secret>
PROXMOX_VE_INSECURE=true
PROXMOX_POOL_ID=pool-user
```

### LLM API Keys (automatic from host environment)

If you have `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` set in your environment, they'll automatically be copied into the VM:

```bash
# On host (where you run terraform)
export OPENAI_API_KEY="sk-..."
export ANTHROPIC_API_KEY="sk-ant-..."

# Use the wrapper script (automatically detects and copies keys)
./apply.sh apply

# Or manually:
terraform apply \
  -var="openai_api_key=$OPENAI_API_KEY" \
  -var="anthropic_api_key=$ANTHROPIC_API_KEY"
```

The VM will then have these available machine-wide. The `apply.sh` wrapper automatically detects these keys and passes them to Terraform.

**This allows the VM to:**

- Manage itself via Proxmox API
- Create sibling VMs in its pool
- Use Terraform with auto-configured Proxmox provider
- Run Proxmox CLI tools (pvesh, qm, etc.)

**Example usage inside VM:**

```bash
# Use environment variables directly
curl -k -H "Authorization: PVEAPIToken=$PROXMOX_VE_API_TOKEN" \
  "$PROXMOX_VE_ENDPOINT/api2/json/cluster/resources?type=vm"

# Or use Terraform (provider picks up PROXMOX_VE_* vars)
terraform init
terraform apply
```

## Custom Environment Variables

You can inject additional environment variables:

```hcl
custom_env_vars = {
  DEBUG       = "1"
  ENVIRONMENT = "development"
  EDITOR      = "vim"
  API_KEY     = "secret"
}
```

These are merged with Proxmox credentials and set machine-wide in NixOS.

## Variables

| Variable          | Description                          | Default                             |
| ----------------- | ------------------------------------ | ----------------------------------- |
| `username`        | Username (Proxmox user and VM user)  | **required**                        |
| `pool_name`       | Resource pool name                   | `pool-{username}`                   |
| `vm_name`         | VM name                              | `{username}-nixos`                  |
| `vm_id`           | VM ID (0 for auto)                   | `0`                                 |
| `vcpus`           | Number of vCPUs                      | `4`                                 |
| `memory_mb`       | Memory in MB                         | `8192`                              |
| `disk_size_gb`    | Disk size in GB                      | `50`                                |
| `nixos_channel`   | NixOS channel (unstable/24.11/24.05) | `unstable`                          |
| `enable_gui`      | Enable GNOME desktop with auto-login | `true`                              |
| `ssh_public_key`  | SSH public key                       | `~/.ssh/id_rsa.pub`                 |
| `ducktape_repo`   | Ducktape repository URL              | `github:agentydragon/ducktape/main` |
| `custom_env_vars` | Additional environment variables     | `{}`                                |

## Usage Examples

### Minimal Configuration

```hcl
username = "alice"
```

### Custom Resources

```hcl
username     = "bob"
vcpus        = 8
memory_mb    = 16384
disk_size_gb = 100
```

### Headless Server

```hcl
username   = "serveruser"
enable_gui = false
```

### Specific NixOS Version

```hcl
username      = "dev"
nixos_channel = "24.11"  # Use stable 24.11
```

## Accessing the Environment

### SSH Access

```bash
# Get VM IP
terraform output vm_ipv4_addresses

# SSH (passwordless with your key)
ssh user@<vm-ip>
```

### Proxmox Web UI

```bash
# View user details
terraform output instructions

# Set password for web UI access
ssh root@atlas "pveum user password {username}@pve"

# Access: https://atlas.agentydragon.com:8006
```

### Console Access

Open Proxmox web UI → VM → Console (auto-login if GUI enabled)

## Home-Manager Management

```bash
# View home-manager generations
home-manager generations

# Update configuration
cd ~/.config/home-manager
home-manager switch --flake .

# Update flake inputs (including ducktape)
nix flake update
home-manager switch --flake .
```

## Outputs

- `pool_id`: Resource pool ID
- `username`: Full Proxmox username
- `vm_name`: VM name
- `vm_id`: VM ID
- `vm_ipv4_addresses`: VM IP addresses (requires QEMU agent)
- `user_api_token`: User's API token (sensitive)
- `instructions`: Detailed setup instructions

## Troubleshooting

### Cloud-init not completing

Check cloud-init status inside VM:

```bash
ssh user@<vm-ip> 'sudo cloud-init status --long'
```

### Home-manager fails to initialize

Check logs:

```bash
ssh user@<vm-ip> 'journalctl -xe | grep home-manager'
```

### VM can't be created (permission denied)

Verify user has permissions on pool:

```bash
ssh root@atlas "pveum aclmod /pool/pool-{username} -user {username}@pve"
```

### SSH key not working

Ensure your public key exists:

```bash
ls -la ~/.ssh/id_rsa.pub
```

Or specify explicitly:

```hcl
ssh_public_key = "ssh-rsa AAAAB3..."
```

## Cleanup

```bash
terraform destroy
```

This removes:

- VM and all disks
- Configuration snippets
- Resource pool
- Proxmox user and tokens
- Permissions

## Advanced

### Multiple Users

Create separate directories or use workspaces:

```bash
# Workspace approach
terraform workspace new alice
terraform apply -var="username=alice"

terraform workspace new bob
terraform apply -var="username=bob"
```

### Custom NixOS Configuration

Edit `configuration.nix.tpl` to customize:

- Additional packages
- System services
- Desktop environment settings
- Network configuration

### Custom Home-Manager Flake

Fork ducktape and set:

```hcl
ducktape_repo = "github:youruser/yourfork/main"
```

## Security Notes

- ⚠️ Self-signed certs accepted (`insecure = true`) - adjust for production
- ⚠️ API tokens without privilege separation - enable for production
- ✅ Passwordless user - secured via SSH keys only
- ✅ Least privilege - VMs created with user credentials, not admin
- ✅ Isolated pools - users cannot access each other's VMs

## Related

- [Proxmox User Pools](../proxmox-user-pools) - Pool management only (no VM)
- [Ducktape Repository](https://github.com/agentydragon/ducktape) - Home-manager configuration
- [bpg/proxmox Provider](https://registry.terraform.io/providers/bpg/proxmox/latest)
