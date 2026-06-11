# rpool Storage Breakdown

**Device**: Sabrent SB-RKT5-4TB NVMe SSD
**Total Size**: 3.6 TB
**Used**: 480 GB (13%)
**Available**: 3.04 TB (87%)

## Top-Level Allocation

| Dataset          | Used        | Purpose                  |
| ---------------- | ----------- | ------------------------ |
| rpool/data       | 439 GB      | VM disks and snapshots   |
| rpool/ROOT       | 23.8 GB     | Proxmox OS               |
| rpool/var-lib-vz | 16.4 GB     | Templates, ISOs, backups |
| **Total**        | **480 GB**  |                          |
| **Free**         | **3.04 TB** |                          |

## VM Disk Breakdown (by VM)

### Running VMs

| VM ID   | Name                | Disk Usage | Notes                                         |
| ------- | ------------------- | ---------- | --------------------------------------------- |
| **100** | **wyrm**            | **277 GB** | Desktop VM (main disk: 223GB + 54GB snapshot) |
| 1500    | talos-controlplane0 | 2.20 GB    | K8s control plane                             |
| 1501    | talos-controlplane1 | 2.05 GB    | K8s control plane                             |
| 1502    | talos-controlplane2 | 1.79 GB    | K8s control plane                             |
| 2000    | talos-worker0       | 4.38 GB    | K8s worker                                    |
| 2001    | talos-worker1       | 3.82 GB    | K8s worker                                    |
| 2002    | talos-worker2       | 4.59 GB    | K8s worker                                    |

**Running VMs subtotal**: 296 GB

### Stopped VMs (Templates/Unused)

| VM ID | Name             | Disk Usage | Status                |
| ----- | ---------------- | ---------- | --------------------- |
| 101   | user-nixos       | 1.45 GB    | Stopped               |
| 102   | nixos-experiment | 18.7 GB    | Stopped               |
| 104   | win11-template   | 16.1 GB    | Template base         |
| 105   | windows11-test   | 16.8 GB    | Stopped               |
| 200   | k3s-master       | 32.2 GB    | Old cluster (stopped) |
| 201   | k3s-worker       | 38.4 GB    | Old cluster (stopped) |
| 202   | k3s-master-2     | 7.83 GB    | Old cluster (stopped) |
| 203   | k3s-worker-2     | 8.29 GB    | Old cluster (stopped) |
| 1301  | linux-desktop-01 | 2.83 GB    | Stopped               |
| 9001  | ubuntu-template  | 1.01 GB    | Template              |

**Stopped VMs subtotal**: 143 GB

## Detailed: wyrm (VM 100) - Largest Consumer

```
vm-100-disk-1:           277 GB total
  ├─ Current data:       223 GB (actual disk contents)
  └─ Snapshot:            54 GB (@pre-reboot)

vm-100-disk-2:           128 KB (EFI disk)
vm-100-disk-3:           228 KB (TPM)
```

**Recommendation**: The 54GB snapshot could be deleted if no longer needed:

```bash
ssh root@atlas 'zfs destroy rpool/data/vm-100-disk-1@pre-reboot'
```

## Space Available for Swap

**Current free**: 3.04 TB
**Proposed swap**: 32 GB (0.01 TB)
**After swap**: 3.01 TB remaining (99.1% available)

**Conclusion**: Adding 32GB swap will use less than 1% of available space. Plenty of room!

## Cleanup Opportunities (Optional)

If you need more space, consider removing:

1. **Old k3s cluster VMs (200-203)**: 86 GB
   - These appear to be from an older cluster before Talos migration

   ```bash
   ssh root@atlas 'for vmid in 200 201 202 203; do qm destroy $vmid; done'
   ```

2. **Stopped Windows VMs (104, 105)**: 33 GB
   - Template + test VM

   ```bash
   ssh root@atlas 'qm destroy 104 105'
   ```

3. **wyrm snapshot**: 54 GB

   ```bash
   ssh root@atlas 'zfs destroy rpool/data/vm-100-disk-1@pre-reboot'
   ```

4. **Old k3s worker snapshots**: 11 GB
   ```bash
   ssh root@atlas 'zfs destroy rpool/data/vm-200-disk-0@pre-terraform-2025-11-03'
   ssh root@atlas 'zfs destroy rpool/data/vm-201-disk-0@pre-terraform-2025-11-03'
   ```

**Potential recovery**: 184 GB (still leaving 2.85 TB free)

## Current Usage Summary

```
Total rpool: 3.6 TB
├─ OS:              23.8 GB (1%)
├─ VM disks:       439 GB (12%)
│  ├─ Running:     296 GB (8%)
│  │  └─ wyrm:     277 GB (largest!)
│  └─ Stopped:     143 GB (4%)
├─ Templates/ISOs:  16.4 GB (0.5%)
└─ Free:          3.04 TB (87%)
```

**Health**: Excellent - 87% free space, no concerns.
