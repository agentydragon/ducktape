# Cloud-Init Network Configuration for Proxmox Nodes

**Date:** 2026-01-03
**Working Directory:** `/home/agentydragon/code/ducktape/cluster`
**Git Branch:** `devel`

## What We Accomplished

### Goal

Add a flag-switched option to configure Proxmox node static IPs via cloud-init snippets instead of
per-node Image Factory schematics. This should significantly reduce bootstrap time by eliminating
per-node image downloads.

**Expected Improvement:**

| Phase            | META (current)                | Cloud-Init (new) |
| ---------------- | ----------------------------- | ---------------- |
| Image download   | 7-9 min × 2 nodes = 14-18 min | 30-60s (once)    |
| Snippet creation | N/A                           | <1s per node     |
| **Total**        | 14-18 min                     | ~1 min           |

### Key Changes

#### 1. Added `proxmox_network_config_method` Variable

- File: `terraform/01-infrastructure/variables.tf:64-72`
- Options: `"meta"` (default, current behavior) or `"cloudinit"` (new snippets approach)

#### 2. Conditional Schematic Resources

- File: `terraform/01-infrastructure/proxmox-nodes.tf:14-111`
- META mode: Per-node schematics with IP baked in via META key 0xa
- CLOUDINIT mode: Single shared schematic with just extensions (no network config)

#### 3. Cloud-Init Network Snippets

- File: `terraform/01-infrastructure/proxmox-nodes.tf:146-172`
- Creates per-node network-config YAML files using netplan v2 format
- Uploaded to Proxmox `local` datastore as snippets

#### 4. Conditional VM Configuration

- File: `terraform/01-infrastructure/proxmox-nodes.tf:218-246`
- Conditional `import_from` for disk (shared vs per-node image)
- Dynamic `initialization` block for cloud-init drive (CLOUDINIT mode only)

#### 5. SSOT Cleanup (Prerequisite)

Cleaned up duplicated Proxmox access configuration:

- **Deleted old monolithic terraform** (via `git rm`):
  - `terraform/main.tf`
  - `terraform/variables.tf`
  - `terraform/outputs.tf`
  - `terraform/terraform.tf`
  - `terraform/modules/` (pve-auth, infrastructure, gitops, dns)

- **Fixed SSH reference** in `terraform/00-persistent-auth/main.tf:56`:
  - Changed from `local.proxmox_host` to `local.proxmox_ssh_target`

- **Updated cleanup script** `terraform/01-infrastructure/scripts/cleanup_proxmox_volumes.py:74-75`:
  - Now accepts FQDN parameter
  - Derives SSH target as `root@{host}` (SSOT pattern)

### SSOT Design Pattern

- **Single source of truth**: `proxmox_api_host` variable (FQDN: `atlas.agentydragon.com`)
- **Derived values** (computed as locals):
  - SSH target: `root@${var.proxmox_api_host}`
  - API endpoint: `https://${var.proxmox_api_host}/`
- **Separate variable**: `proxmox_node_name` (stays as "atlas" - needed for Proxmox API node parameter)

## Incomplete Work / Open Threads

### CRITICAL: Changes Not Yet Committed

The following cluster changes need to be committed before testing:

```bash
git add terraform/00-persistent-auth/main.tf \
        terraform/00-persistent-auth/variables.tf \
        terraform/01-infrastructure/cilium.tf \
        terraform/01-infrastructure/main.tf \
        terraform/01-infrastructure/proxmox-nodes.tf \
        terraform/01-infrastructure/scripts/cleanup_proxmox_volumes.py \
        terraform/01-infrastructure/variables.tf \
        terraform/01-infrastructure/wait-for-k8s-api.sh \
        terraform/main.tf terraform/modules/ \
        terraform/outputs.tf terraform/terraform.tf terraform/variables.tf

git commit -m "feat: add cloud-init network config option for Proxmox nodes

- Add proxmox_network_config_method variable ('meta' or 'cloudinit')
- Implement conditional schematic resources (per-node vs shared)
- Create cloud-init network snippets for CLOUDINIT mode
- Add dynamic initialization block for cloud-init CD
- Delete old monolithic terraform structure (modules/*, main.tf)
- Fix SSOT: use proxmox_api_host FQDN consistently
- Update cleanup_proxmox_volumes.py to use FQDN parameter"
```

### Testing Required

1. **Set `proxmox_network_config_method = "cloudinit"` in tfvars or via `-var`**
2. **Run full destroy/bootstrap cycle**:

   ```bash
   cd terraform/01-infrastructure
   terraform destroy -auto-approve
   ./bootstrap.sh
   ```

3. **Verify**:
   - Single image downloaded (check Proxmox storage)
   - Nodes boot with correct IPs
   - Nodes join cluster successfully
4. **Compare timing** vs META approach

### Rollback

Set `proxmox_network_config_method = "meta"` to revert to current behavior.

## Context for Successor Agents

### Project Conventions

- See: `@CLAUDE.md` for full cluster instructions
- Layered terraform: 00-persistent-auth → 01-infrastructure → 02-services → 03-configuration
- **Never destroy layer 00** without explicit user authorization

### Key Files

| File                                             | Purpose                                                |
| ------------------------------------------------ | ------------------------------------------------------ |
| `terraform/01-infrastructure/proxmox-nodes.tf`   | All Proxmox node definitions (schematics, images, VMs) |
| `terraform/01-infrastructure/variables.tf:64-72` | The `proxmox_network_config_method` switch             |
| `terraform/00-persistent-auth/main.tf`           | SSH target derivation pattern                          |

### Build/Test

```bash
# From cluster directory
cd terraform/01-infrastructure
terraform validate
terraform plan -var="proxmox_network_config_method=cloudinit"
```

### Important References

- Plan file: `~/.claude/plans/sassy-seeking-blossom.md` (detailed implementation plan)
- Talos NoCloud platform docs: Talos reads network-config from cloud-init CD

## Related Documentation

- `docs/plan.md` - Cluster roadmap
- `docs/bootstrap.md` - Bootstrap procedures
