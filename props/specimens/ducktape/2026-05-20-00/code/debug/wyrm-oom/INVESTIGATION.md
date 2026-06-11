# Wyrm VM OOM Investigation

**Last Updated**: 2025-12-09
**Status**: ⚠️ ONGOING - Monitoring after virtiofs fix became active
**VM**: wyrm (VM 100) on atlas Proxmox host

## Documentation Structure

- **`CLAUDE.md` (this file)** - Comprehensive guide for agents working on this issue
- **`LOG.md`** - **Chronological log of all events** (append-only, always add timestamp) - **PRIMARY RECORD**
- **`HOWTO-fix-memory.md`** - Step-by-step guide for memory fixes
- **`scripts/`** - Diagnostic and monitoring scripts
- **`logs/`** - Timestamped script outputs and analysis results
- **`notes/`** - Supporting documentation (architecture, setup guides)

## IMPORTANT: Updating LOG.md

**LOG.md is the chronological record of this investigation.** When making updates:

1. **Always append** (never insert in middle or overwrite)
2. **Always start with timestamp**: Use `date` command output
3. **Format**: `## $(date)` as header, then your content
4. Keep entries top-to-bottom chronological (oldest first)

```bash
# Example:
echo "" >> LOG.md
echo "## $(date)" >> LOG.md
echo "" >> LOG.md
echo "**Your update title**" >> LOG.md
echo "" >> LOG.md
echo "Details..." >> LOG.md
```

See LOG.md for full history of this issue.

## Current Situation

The wyrm VM experienced 5 OOM kills between Nov 29 and Dec 9. A virtiofs memory leak was identified and fixed on Dec 1, but the VM wasn't restarted until Dec 9 (after the 5th OOM). The fix is now active and system has been stable for 4+ hours. Monitoring continues to confirm long-term stability.

## Quick Reference

| Component   | Value                     | Status                                 |
| ----------- | ------------------------- | -------------------------------------- |
| **Host**    | atlas (Proxmox VE 8.4.14) | 128GB RAM, no swap                     |
| **VM**      | wyrm (VM 100)             | 64GB RAM allocation                    |
| **Mount**   | /code                     | Currently virtiofs with cache=metadata |
| **Problem** | Recurring OOM kills       | Every 48-72 hours                      |

## Investigation History

### Phase 1: virtiofs Memory Leak (2025-12-01)

**Problem**: virtiofsd was using default `cache=auto` policy, leading to unbounded file descriptor accumulation.

**Evidence**:

- 595,039 open file descriptors
- 12GB RSS after 35 hours uptime
- Memory growing at ~340MB/hour

**Fix Applied**: Changed virtiofs cache policy to `cache=metadata`

```bash
qm set 100 --virtiofs1 code,cache=metadata
qm set 100 --virtiofs0 tankshare,cache=metadata
```

**Expected Outcome**: FD count <1000, RSS <500MB
**Actual Outcome**: OOM issues persist (current investigation)

### Phase 2: virtiofs Architecture Limitations (2025-12-03)

**Problem**: virtiofs doesn't support MAP_SHARED mmap (FUSE limitation)

**Impact**: Breaks devenv/Nix tooling that requires MAP_SHARED for Git operations

**Proposed Solution**: Migrate from virtiofs to NFS

- NFS provides full POSIX semantics including MAP_SHARED
- Trade-off: 3-5x higher latency (10µs → 50µs) but still acceptable
- Benefits: Mature, stable, no memory leaks

**Status**: Migration not yet performed

### Phase 3: Current Investigation (2025-12-09)

**Problem**: System continues to OOM despite virtiofs cache fix

**Possible Causes**:

1. Cache fix didn't take effect (VM not restarted?)
2. Different memory leak source
3. System structurally overcommitted:
   - VM allocations: 100GB (wyrm 64GB + k3s VMs 36GB)
   - ZFS ARC: 12GB
   - Host overhead: ~4GB
   - Total: ~116GB of 128GB (92% baseline utilization)
4. Another process leaking memory
5. ZFS ARC not properly capped

## System Architecture

### Host (atlas)

- **CPU**: AMD Ryzen 9 9950X3D (16C/32T)
- **RAM**: 128GB DDR5 (no swap configured!)
- **OS**: Proxmox VE 8.4.14, kernel 6.8.12-16-pve
- **Storage**:
  - `rpool`: 3.62TB ZFS (NVMe) - OS, VM disks
  - `tank`: 58.2TB ZFS RAIDZ2 (HDD) - shared storage

### VMs on atlas

- **VM 100 (wyrm)**: 64GB RAM, 32 vCPUs - Main desktop VM
- **VM 1500-1502** (talos-controlplane): 12GB each - K8s control plane
- **VM 2000-2002** (talos-worker): 12GB each - K8s workers

**Total Allocation**: 64 + 36 + 12 (ARC) + 16 (overhead) = 128GB (at capacity!)

### Current Mount Configuration

```bash
# On host (atlas)
virtiofsd --fd=15 --shared-dir=/tank/code --cache=metadata --announce-submounts --syslog

# In guest (wyrm)
/code type virtiofs (rw,relatime)
```

## Diagnostic Commands

### Check Current Memory State

```bash
# On wyrm (this VM)
ssh root@atlas 'free -h'

# Check virtiofsd processes
ssh root@atlas 'ps aux | grep virtiofsd'

# Check file descriptor counts
ssh root@atlas 'for pid in $(pgrep virtiofsd); do echo "PID $pid: $(ls /proc/$pid/fd 2>/dev/null | wc -l) FDs"; done'

# Check VM memory allocations
ssh root@atlas 'qm list'
ssh root@atlas 'qm config 100 | grep -E "memory|cores"'
```

### Check OOM History

```bash
# Recent OOM kills
ssh root@atlas 'dmesg -T | grep -i "killed process" | tail -20'

# System memory pressure over time (if prometheus monitoring exists)
# See monitoring setup section below
```

### Check ZFS ARC

```bash
# Current ARC size
ssh root@atlas 'cat /proc/spl/kstat/zfs/arcstats | grep -E "^size|^c_max"'

# ARC settings
ssh root@atlas 'cat /sys/module/zfs/parameters/zfs_arc_max'
```

### Verify virtiofs Cache Setting

```bash
# Check if cache=metadata is actually in use
ssh root@atlas 'ps aux | grep virtiofsd | grep code'
# Should show: --cache=metadata

# If it doesn't, the VM may not have been restarted after config change
ssh root@atlas 'qm config 100 | grep virtiofs'
```

## Immediate Actions to Take

### 1. Check if virtiofs fix actually took effect

```bash
# On host
ssh root@atlas 'ps aux | grep "virtiofsd.*code"'
```

**If you DON'T see `--cache=metadata`**: The VM needs to be restarted for the fix to take effect.

```bash
# Restart VM 100 to apply config
ssh root@atlas 'qm shutdown 100 && qm wait 100 && qm start 100'
```

**If you DO see `--cache=metadata`**: The fix is active, but there's another problem.

### 2. Add Emergency Swap (CRITICAL)

The system has **zero swap** which means any memory spike immediately triggers OOM killer.

```bash
ssh root@atlas << 'EOF'
# Create 32GB swap on fast NVMe
zfs create -V 32G -o compression=zle rpool/swap
mkswap /dev/zvol/rpool/swap
swapon /dev/zvol/rpool/swap

# Make persistent
echo '/dev/zvol/rpool/swap none swap defaults 0 0' >> /etc/fstab

# Set low swappiness (only swap under severe pressure)
sysctl vm.swappiness=10
echo 'vm.swappiness=10' >> /etc/sysctl.d/99-swappiness.conf
EOF
```

**Why**: This gives ~3 days runway before OOM even if leak continues.

### 3. Reduce ZFS ARC Cap

Currently ARC can use up to 12.3GB. Reduce to 8GB to free memory.

```bash
ssh root@atlas << 'EOF'
# Immediate
echo 8589934592 > /sys/module/zfs/parameters/zfs_arc_max  # 8GB

# Persistent across reboots
echo 'options zfs zfs_arc_max=8589934592' > /etc/modprobe.d/zfs.conf
update-initramfs -u
EOF
```

**Benefit**: Frees ~4GB for VMs and host processes.

### 4. Monitor Memory Growth

```bash
# Watch virtiofsd memory in real-time
ssh root@atlas 'watch -n 10 "ps aux --sort=-%mem | grep virtiofsd | head -5"'

# Watch file descriptor count
ssh root@atlas 'watch -n 30 "for pid in \$(pgrep virtiofsd); do echo \"PID \$pid: \$(ls /proc/\$pid/fd 2>/dev/null | wc -l) FDs\"; done"'

# Watch total system memory
ssh root@atlas 'watch -n 10 "free -h"'
```

## Long-term Solutions

### Option 1: Migrate to NFS (Recommended)

Replace virtiofs with NFS to get:

- Full POSIX semantics (MAP_SHARED mmap support)
- No memory leak issues
- Battle-tested stability
- Slight performance trade-off (acceptable for development workload)

**Setup**:

```bash
# On host (atlas)
ssh root@atlas << 'EOF'
apt update && apt install nfs-kernel-server
echo "/tank/code *(rw,sync,no_subtree_check,no_root_squash,fsid=0)" >> /etc/exports
exportfs -ra
systemctl enable --now nfs-kernel-server
EOF

# On guest (wyrm) - test first
sudo mkdir -p /mnt/code-nfs
sudo mount -t nfs atlas:/tank/code /mnt/code-nfs -o vers=4.2,rsize=1048576,wsize=1048576

# Test devenv works
cd /mnt/code-nfs/gitlab.com/agentydragon/ducktape/adgn
devenv shell  # Should work!

# If successful, update /etc/fstab and remove virtiofs
```

**Migration timeline**: 1-2 hours including testing

### Option 2: Add More RAM to Host

Current: 128GB at 92% utilization (structurally tight)
Recommended: 192GB or 256GB

**Cost**: ~$300-500 for 64GB DDR5
**Benefit**: Eliminates capacity constraints, allows growth

### Option 3: Reduce VM Allocations

If RAM upgrade not possible:

- Reduce wyrm from 64GB to 48GB (if workload permits)
- Or shut down some k3s worker VMs during development

## Monitoring Setup

### Deploy Host Monitoring (Recommended)

```bash
# On atlas
ssh root@atlas << 'EOF'
apt update && apt install prometheus-node-exporter
systemctl enable --now prometheus-node-exporter
EOF

# In your k3s cluster, add scrape config for atlas:
# (See notes/monitoring-config.yaml for full example)
```

### Key Metrics to Track

1. **Host memory available**: Should stay >10% free
2. **virtiofsd RSS**: Alert if >2GB per process
3. **virtiofsd FD count**: Alert if >100k
4. **OOM kill events**: Track frequency
5. **Memory growth rate**: Predict time to OOM

### Alert Thresholds

```yaml
# Critical: Memory under 5% available
# Warning: Memory under 10% available
# Warning: virtiofsd RSS > 2GB
# Critical: virtiofsd FDs > 500k
```

## Scripts Available

### `scripts/check-memory.sh`

Checks current memory state of host and all VMs

### `scripts/check-virtiofsd.sh`

Inspects virtiofsd processes (FDs, memory, cache policy)

### `scripts/check-oom-history.sh`

Extracts OOM kill events from dmesg

### `scripts/monitor-continuous.sh`

Continuous monitoring loop (run in tmux/screen)

## Notes and Documentation

### `notes/virtiofs-vs-nfs.md`

Detailed comparison of virtiofs and NFS architectures, explaining why virtiofs is faster but has limitations (MAP_SHARED mmap not supported).

### `notes/nfs-setup-guide.md`

Step-by-step guide for migrating from virtiofs to NFS

### `notes/previous-findings.md`

Historical investigation results from December 1-3, 2025

## Logs

### `logs/YYYY-MM-DD_HH-MM-memory-snapshot.txt`

Timestamped memory state snapshots

### `logs/YYYY-MM-DD_HH-MM-oom-events.txt`

OOM kill events extracted from system logs

## Decision Points for Future Agents

### Should you restart the VM immediately?

**Yes, if**:

- virtiofsd is not showing `--cache=metadata` flag
- FD count is >100k
- Memory leak is actively growing

**No, if**:

- User is actively working (ask first)
- You want to capture diagnostic data first

### Should you migrate to NFS immediately?

**Yes, if**:

- Need devenv/Nix to work (currently broken on virtiofs)
- OOM issues persist despite virtiofs fix
- User approves downtime for migration

**No, if**:

- Want to exhaust virtiofs debugging first
- Need to coordinate scheduled downtime

### Should you add swap immediately?

**YES, ALWAYS** - There's no reason not to have swap on a hypervisor. This is emergency buffer that should have existed from day one.

## Common Pitfalls

1. **Forgetting to restart VM after config changes**: Proxmox `qm set` changes config file but doesn't affect running VMs
2. **Not checking if cache=metadata is actually active**: Use `ps aux | grep virtiofsd` to verify
3. **Assuming swap is enabled**: Proxmox doesn't create swap by default
4. **Ignoring structural overcommit**: Even without leaks, system is at 92% capacity

## References

- Proxmox virtiofs docs: https://pve.proxmox.com/wiki/VirtIO-FS
- virtiofsd source: https://gitlab.com/virtio-fs/virtiofsd
- NFS vs virtiofs architecture: See `notes/virtiofs-vs-nfs.md`
- Previous investigation: See `notes/previous-findings.md`

## Questions to Answer

For the next agent working on this:

1. **Has the VM been restarted since cache=metadata was set?**
   - Check with: `ssh root@atlas 'qm status 100'` (look at uptime)
   - Check with: `ssh root@atlas 'ps aux | grep virtiofsd'` (look for --cache flag)

2. **Is there swap now?**
   - Check with: `ssh root@atlas 'swapon --show'`

3. **What's the current FD count?**
   - Check with diagnostic commands above

4. **When was the last OOM event?**
   - Check with: `ssh root@atlas 'dmesg -T | grep "killed process" | tail -5'`

5. **Is NFS migration still needed?**
   - Yes, if devenv/Nix is required (MAP_SHARED mmap issue)
   - Maybe, if OOM issues persist despite virtiofs fix

## Contact and Escalation

If you're stuck:

1. Check `notes/` directory for detailed technical background
2. Run all diagnostic commands and capture output to `logs/`
3. Ask user about acceptable downtime for fixes
4. Consider migration to NFS as most reliable solution

---

**This is a living document. Update it as you learn more about the OOM issue.**
