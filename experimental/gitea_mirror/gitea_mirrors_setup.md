# Gitea Mirrors Setup - Public Repository Mirror Instance

## Overview

Setting up a dedicated Gitea instance (`gitea-mirrors`) to host public repository mirrors, enabling efficient git cloning with `--reference` across all VMs. This creates a separate storage slice from tankshare specifically for git mirrors.

## Architecture

### Storage Architecture (Option C - virtiofs)

- **ZFS Dataset**: `tank/gitea-public-mirrors` on atlas (200GB quota)
- **Mount Strategy**: virtiofs shared filesystem to k3s VMs
- **K8s Storage**: hostPath PV on k3s-master at `/mnt/gitea-mirrors`
- **Separate from tankshare**: Dedicated storage slice for git repositories

### Network Architecture

- **Domain**: `mirrors.git.k3s.agentydragon.com`
- **Access**: Public read-only for cloning, admin-only for mirror management
- **Integration**: Works with existing Authentik SSO

## Implementation Status

### ✅ Completed Tasks

1. **Restored helmfile.yaml changes**
   - Re-added cluster-infrastructure, webhook-inbox, gitea-mirrors releases
   - Added OpenEBS repositories for ZFS CSI
   - Set timeout override for gitea-mirrors (120s)

2. **Fixed gitea-mirrors Helm chart**
   - Updated repository URL from dl.gitea.com to dl.gitea.io
   - Fixed values passing to gitea subchart
   - Configured persistence.create: false to prevent duplicate PVC
   - Set persistence.claimName: gitea-mirrors-data

3. **Fixed storage configuration**
   - Changed from ZFS CSI to hostPath (ZFS not available on k3s VMs)
   - Updated node topology from "atlas" to "k3s-master"
   - Configured hostPath at `/mnt/gitea-mirrors`

4. **Created Ansible automation**
   - `ansible/roles/gitea-mirrors/`: Role for ZFS dataset creation
   - `ansible/roles/gitea-mirrors/files/virtiofsd-gitea-mirrors.service`: systemd service
   - Added gitea-mirrors role to atlas.yaml playbook
   - Updated k3s-nodes.yaml with mount configuration

5. **Configured virtiofs automation**
   - Automated VM configuration with `qm set` commands
   - Created systemd service for virtiofsd daemon
   - Added fstab entries and mount commands for k3s nodes

### 🔄 Pending Deployment Steps

1. **Deploy ZFS dataset on atlas**

   ```bash
   cd ~/code/ducktape/ansible
   cd ansible
   ansible-playbook atlas.yaml --tags gitea-mirrors
   ```

   This will:
   - Create ZFS dataset `tank/gitea-public-mirrors` with 200GB quota
   - Configure virtiofs for k3s VMs (vmid 200, 201)
   - Install and start virtiofsd systemd service

2. **Configure mounts on k3s nodes**

   ```bash
   cd ansible
   ansible-playbook k3s-nodes.yaml --tags gitea-mirrors
   ```

   This will:
   - Create `/mnt/gitea-mirrors` mount point
   - Add virtiofs entry to /etc/fstab
   - Mount the filesystem

3. **Deploy Gitea Mirrors Helm chart**

   ```bash
   cd ~/code/ducktape/k8s/helmfile
   helmfile -l name=gitea-mirrors sync
   ```

   This will:
   - Create gitea-mirrors namespace
   - Deploy hostPath PV/PVC
   - Install Gitea with SQLite backend
   - Configure as public mirror instance

4. **Bootstrap mirror repositories**
   - Use existing gitea_mirror MCP server
   - Create mirrors for commonly used repositories
   - Test with initial set of repositories

5. **Test git clone with reference**

   ```bash
   # After mirrors are populated
   git clone --reference /mnt/gitea-mirrors/github.com/torvalds-linux.git \
     https://github.com/torvalds/linux
   ```

## Configuration Details

### ZFS Dataset Settings

```bash
recordsize=128k     # Optimal for git objects
compression=lz4     # Fast compression
dedup=off          # Not needed for git
atime=off          # Performance optimization
quota=200G         # Storage limit
```

### Gitea Configuration

- **Database**: SQLite3 (simple, sufficient for mirrors)
- **Domain**: mirrors.git.k3s.agentydragon.com
- **Features**:
  - Mirroring enabled (1h default, 10m minimum interval)
  - Registration disabled
  - Public viewing enabled
  - SSH disabled
  - Landing page: explore

### Kubernetes Resources

- **Namespace**: gitea-mirrors
- **Storage**: 200Gi hostPath PV on k3s-master
- **Service**: ClusterIP (exposed via Traefik)
- **Dependencies**: cluster-infrastructure (for potential CSI drivers)

## File Structure

```
ducktape/
├── ansible/
│   ├── atlas.yaml                           # Updated with gitea-mirrors role
│   ├── k3s-nodes.yaml                               # Updated with mount configuration
│   └── roles/
│       └── gitea-mirrors/
│           ├── tasks/main.yml                       # ZFS creation, virtiofs setup
│           ├── files/virtiofsd-gitea-mirrors.service # systemd service
│           └── handlers/main.yml                    # systemd reload handler
├── k8s/
│   ├── helm/
│   │   └── gitea-mirrors/
│   │       ├── Chart.yaml                          # Wrapper chart
│   │       ├── values.yaml                         # hostPath configuration
│   │       └── templates/
│   │           └── storage.yaml                    # PV/PVC definitions
│   └── helmfile/
│       ├── helmfile.yaml                           # Added gitea-mirrors release
│       └── values/
│           └── gitea-mirrors.yaml                  # Helm values overrides
└── docs/
    └── gitea-mirrors-setup.md                      # This document
```

## Benefits

1. **Efficient Cloning**: Use `--reference` to share git objects
2. **Network Savings**: Reduce bandwidth for common repositories
3. **Fast VM Provisioning**: Quick access to frequently used code
4. **Centralized Updates**: Single location for mirror updates
5. **ZFS Features**: Snapshots, compression, quotas

## Troubleshooting

### If VMs can't mount virtiofs

1. Check virtiofsd service: `systemctl status virtiofsd-gitea-mirrors`
2. Verify VM args: `qm config 200 | grep virtiofs`
3. Check socket: `ls -la /var/run/virtiofsd-gitea-mirrors.sock`

### If Helm deployment fails

1. Check PVC status: `kubectl get pvc -n gitea-mirrors`
2. Verify node mount: `ssh k3s-master ls -la /mnt/gitea-mirrors`
3. Check pod logs: `kubectl logs -n gitea-mirrors deployment/gitea-mirrors`

### If mirrors don't update

1. Check Gitea logs: `kubectl logs -n gitea-mirrors deployment/gitea-mirrors`
2. Verify network connectivity from pod
3. Check mirror settings in Gitea admin panel

## Next Steps After Deployment

1. Configure regular mirror updates via cron/systemd timer
2. Add monitoring for disk usage and mirror freshness
3. Document git alias for developers to use `--reference`
4. Consider adding more storage if 200GB proves insufficient
5. Set up backup strategy for mirror configuration

## Related Documentation

- [Ansible k3s role documentation](../ansible/roles/k3s/README.md)
- [Helmfile documentation](../k8s/helmfile/README.md)
- [Git clone --reference documentation](https://git-scm.com/docs/git-clone#Documentation/git-clone.txt---reference)
