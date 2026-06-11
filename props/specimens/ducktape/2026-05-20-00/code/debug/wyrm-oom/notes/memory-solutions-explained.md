# Memory Solutions Explained

## Current Situation

**Host (atlas)**:

- Physical RAM: 128 GB
- VM allocations: 136 GB (overcommitted by 8GB)
- ZFS ARC: 12 GB
- No swap

**Guest (wyrm)**:

- Allocated: 64 GB
- Actually using: ~10 GB
- Free: 52 GB (wasted from host perspective!)

## What is ZFS ARC?

**ZFS ARC** = Adaptive Replacement Cache

- ZFS's intelligent RAM cache for disk data
- Speeds up repeated reads from disk
- Current: 12 GB (can grow up to 12.34 GB max)
- Location: Host memory

**Why it matters**: That 12GB is unavailable for VMs. On an overcommitted system, we need to reduce it.

## Solution 1: Reduce wyrm RAM (EASIEST - 2 minutes)

**Current**: 64 GB allocated, ~10 GB used
**Proposal**: Reduce to 32 GB (still 3x what you're using!)

```bash
# On host (atlas):
ssh root@atlas 'qm set 100 --memory 32768'
ssh root@atlas 'qm shutdown 100 && qm start 100'
```

**Impact**: Frees 32 GB on host immediately
**Risk**: Very low - you're only using 10GB
**Benefit**: System goes from 136GB allocated to 104GB (fits in 128GB!)

## Solution 2: Memory Ballooning

### What is Memory Ballooning?

Memory ballooning lets the **host reclaim unused memory from guests**.

**How it works**:

1. Guest has 64GB allocated, only using 10GB
2. Host needs memory
3. Host tells balloon driver in guest: "inflate to 20GB"
4. Balloon driver allocates 20GB inside guest (making it unavailable)
5. Host reclaims that 20GB physical RAM
6. Now host can use that 20GB for other VMs

**It's like**: Guest says "I need 64GB" but host says "I'll only give you what you actually use"

### Requirements

**Guest side**:

- Balloon driver in kernel (Linux has this built-in)
- qemu-guest-agent running

**Host side**:

- Balloon device enabled in VM config

### Your Status

```bash
# Checked on atlas:
qm config 100 | grep agent
# Output: agent: 1

# This means:
✅ qemu-guest-agent is enabled
✅ Guest can communicate with host
❓ Balloon device - need to check
```

**To check if balloon is enabled**:

```bash
ssh root@atlas 'qm config 100 | grep balloon'
```

**If not enabled, to enable**:

```bash
ssh root@atlas 'qm set 100 --balloon 0'
# balloon: 0 means "minimum 0 MB, can shrink to 0"
```

**How to use**:

- Automatic: Proxmox can auto-balance between VMs
- Manual: Set lower balloon value to force guest to give back memory

**Benefit**: Host can reclaim your 52GB free memory dynamically
**Risk**: Guest might need to swap if balloon gets too aggressive

## Solution 3: Add Swap on Host (30 minutes)

### Disk Layout

```
Host storage:
- rpool: 3.04 TB available (NVMe - FAST)
  - Contains: OS, VM disks
  - Speed: ~3-5 GB/s sequential

- tank: 29 TB available (HDD RAIDZ2 - SLOW)
  - Contains: /code, /share
  - Speed: ~500 MB/s sequential
```

### Where to Add Swap

**Best option**: ZFS volume on rpool (fast NVMe)

**Why**:

- Fast enough for emergency swap
- Plenty of space (3TB available)
- VM disks already there
- ZFS compression helps (swap data compresses well)

**Commands** (NOT executed yet):

```bash
# On atlas:

# 1. Create 32GB ZFS volume for swap
zfs create -V 32G -o compression=lz4 rpool/swap

# 2. Format as swap
mkswap /dev/zvol/rpool/swap

# 3. Enable swap
swapon /dev/zvol/rpool/swap

# 4. Make permanent
echo '/dev/zvol/rpool/swap none swap defaults 0 0' >> /etc/fstab

# 5. Set low swappiness (only swap under pressure)
sysctl vm.swappiness=10
echo 'vm.swappiness=10' >> /etc/sysctl.d/99-swappiness.conf

# 6. Verify
swapon --show
free -h
```

**Impact**:

- Adds 32GB emergency buffer
- System can now handle 136GB + 32GB = 168GB before OOM
- Allows safe overcommit
- Slower than RAM but infinitely better than OOM kills

**Performance**:

- NVMe swap: ~2-3 GB/s read/write
- Adds ~50-100µs latency if pages swapped
- With vm.swappiness=10, rarely used unless pressure

### Why Not tank (HDD)?

- Too slow (500 MB/s vs 3000 MB/s)
- Already used for data storage
- Worse for swap performance

## Solution 4: Reduce ZFS ARC (5 minutes)

**Current**: 12 GB max
**Proposal**: Reduce to 6-8 GB

```bash
# On atlas:

# Immediate reduction
echo 6442450944 > /sys/module/zfs/parameters/zfs_arc_max  # 6GB

# Make permanent
echo 'options zfs zfs_arc_max=6442450944' > /etc/modprobe.d/zfs.conf
update-initramfs -u

# Verify
cat /sys/module/zfs/parameters/zfs_arc_max
```

**Impact**: Frees 6 GB for VMs
**Risk**: Slightly slower disk access (more reads from disk)
**Benefit**: On overcommitted system, VMs > disk cache

## Recommended Action Plan

### Quick Fix (5 minutes):

1. **Reduce wyrm RAM to 32GB** (frees 32GB instantly)
2. **Reduce ZFS ARC to 6GB** (frees 6GB instantly)

**Result**: 104GB VM + 6GB ARC = 110GB / 128GB (comfortable!)

### Long-term (30 minutes):

3. **Add 32GB swap** (emergency buffer)
4. **Enable memory ballooning** (dynamic reclaim)

### Comparison

| Solution        | Time   | Impact          | Risk                   |
| --------------- | ------ | --------------- | ---------------------- |
| Reduce wyrm RAM | 2 min  | -32GB demand    | Very low (using 10GB)  |
| Reduce ZFS ARC  | 5 min  | -6GB demand     | Low (slight perf hit)  |
| Add swap        | 30 min | +32GB capacity  | None                   |
| Enable balloon  | 5 min  | Dynamic reclaim | Low (needs monitoring) |

## Memory Ballooning Deep Dive

### Architecture

```
┌─────────────────────────────────┐
│  Guest (wyrm)                   │
│                                 │
│  Applications: 4GB              │
│  Buff/cache: 6GB                │
│  Free: 52GB ←─────┐             │
│                    │             │
│  Balloon Driver    │ (can inflate)
│  (virtio-balloon)  │             │
└────────────┬───────┴─────────────┘
             │ talks via
             │ qemu-guest-agent
┌────────────┴─────────────────────┐
│  Host (atlas)                    │
│                                  │
│  KVM: 64GB allocated             │
│  Balloon controller ─────────────┘
│  (can request: "inflate to 20GB")
│                                  │
│  Result: Host reclaims 20GB      │
└──────────────────────────────────┘
```

### How to Check Balloon Status

```bash
# On host
ssh root@atlas 'qm config 100 | grep balloon'

# On guest (wyrm)
cat /proc/meminfo | grep -i balloon
lsmod | grep balloon
```

### Enabling Balloon

```bash
# On host
ssh root@atlas 'qm set 100 --balloon 0'
# 0 = can shrink to 0 MB (host can reclaim all free memory)

# Or with minimum:
ssh root@atlas 'qm set 100 --balloon 16384'
# 16384 = keep at least 16GB, can reclaim rest
```

### Automatic Ballooning

Proxmox can automatically balance memory between VMs.

**To enable**:
Edit `/etc/pve/datacenter.cfg`:

```
memory: autobalancing
```

**How it works**:

- Proxmox monitors each VM's memory usage
- Inflates balloons on VMs with lots of free memory
- Deflates balloons on VMs under pressure
- Dynamically shares physical RAM between VMs

### Manual Ballooning

Force guest to use less memory:

```bash
# Current allocation: 64GB
# Set balloon to 32GB minimum
ssh root@atlas 'qm set 100 --balloon 32768'
ssh root@atlas 'qm reboot 100'

# Guest will only be able to use 32GB
# Host reclaims the other 32GB
```

## Why Multiple Solutions?

**Defense in depth**:

1. **Reduce wyrm RAM** - Fixes overcommit baseline
2. **Add swap** - Emergency buffer for spikes
3. **Enable balloon** - Dynamic reclaim for efficiency
4. **Reduce ARC** - Extra headroom

All together:

- Baseline: 104GB (fits in 128GB)
- Balloon: Can reclaim 40-50GB dynamically
- Swap: 32GB emergency buffer
- **Total capacity**: ~160GB effective

**Result**: No more OOMs, efficient memory use.

## Questions?

**Q: Will reducing wyrm RAM hurt performance?**
A: No - you're only using 10GB. Even 32GB gives you 3x headroom.

**Q: Will swap make system slow?**
A: With swappiness=10 and NVMe, only used under extreme pressure. Better than OOM kills!

**Q: Will ZFS ARC reduction hurt performance?**
A: Slightly - more disk reads. But VMs > disk cache on overcommitted systems.

**Q: Does balloon require changes in guest?**
A: No - Linux has virtio-balloon built-in, qemu-guest-agent already running.

**Q: Can we do all of these?**
A: Yes! They're complementary, not mutually exclusive.
