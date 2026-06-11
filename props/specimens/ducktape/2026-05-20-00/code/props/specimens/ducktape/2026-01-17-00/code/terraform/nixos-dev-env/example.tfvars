# Example terraform.tfvars for NixOS Dev Environment
# Copy this to terraform.tfvars and customize

# Username (required) - will be used for both Proxmox user and VM user
username = "testuser"

# Optional: Custom pool name (defaults to pool-{username})
# pool_name = "dev-pool"

# Optional: Custom VM name (defaults to {username}-nixos)
# vm_name = "my-nixos-dev"

# Optional: VM ID (0 for auto-assignment)
# vm_id = 9001

# Optional: VM resources
# vcpus      = 4
# memory_mb  = 8192
# disk_size_gb = 50

# Optional: NixOS channel (unstable, 24.11, 24.05)
# nixos_channel = "unstable"

# Optional: GUI settings (defaults to enabled with GNOME auto-login)
# enable_gui = true

# Optional: SSH key (will use ~/.ssh/id_rsa.pub if not specified)
# ssh_public_key = "ssh-rsa AAAAB3NzaC1yc2E..."

# Optional: Ducktape repository
# ducktape_repo = "github:agentydragon/ducktape/main"

# Optional: Proxmox settings (defaults match main setup)
# proxmox_host     = "atlas"
# proxmox_api_host = "atlas.agentydragon.com"
# proxmox_node_name = "atlas"
