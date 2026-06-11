# Monitoring Scripts

These scripts help diagnose and monitor OOM issues on the wyrm VM running on atlas Proxmox host.

## Quick Start

```bash
cd ~/code/ducktape/wyrm-oom-investigation/scripts

# Run this first - gives quick overview
./quick-check.sh

# For detailed analysis
./check-memory.sh      # Host and VM memory state
./check-virtiofsd.sh   # virtiofsd process analysis
./check-oom-history.sh # Historical OOM events

# For continuous monitoring (run in tmux/screen)
./monitor-continuous.sh
```

## Script Descriptions

### `quick-check.sh`

**Purpose**: Fast health check - run this first
**Output**:

- Memory usage percentage
- Swap status
- virtiofsd FD and memory usage
- Recent OOM events
- VM status

**Use when**: You want a quick status overview

### `check-memory.sh`

**Purpose**: Detailed memory analysis
**Output**:

- Host memory state (free -h)
- ZFS ARC usage and limits
- Swap configuration
- VM memory allocations
- Top memory consumers on host
- Total allocation estimate

**Use when**: You need to understand memory distribution

### `check-virtiofsd.sh`

**Purpose**: Analyze virtiofsd processes specifically
**Output**:

- All virtiofsd processes and their command lines
- File descriptor counts per process
- Memory usage per process
- VM virtiofs configuration
- Whether cache policy is active

**Use when**: Investigating virtiofsd memory leaks

### `check-oom-history.sh`

**Purpose**: Extract OOM kill events from system logs
**Output**:

- Recent OOM kill events with timestamps
- Statistics on which processes were killed
- Memory state at last OOM event
- VM uptime for correlation

**Use when**: Understanding when and why OOMs happened

### `monitor-continuous.sh`

**Purpose**: Long-term monitoring with logging
**Behavior**:

- Runs every 60 seconds
- Logs snapshots to `../logs/`
- Prints summary to terminal
- Captures OOM events automatically when memory >90%

**Use when**: You want to track memory growth over hours/days

**How to run**:

```bash
# Start in tmux/screen so it survives disconnection
tmux new -s memory-monitor
./monitor-continuous.sh

# Detach with Ctrl+B, D
# Reattach with: tmux attach -t memory-monitor
```

## Output Locations

- **Continuous monitoring logs**: `../logs/YYYY-MM-DD_HH-MM-SS_memory-snapshot.txt`
- **OOM event captures**: `../logs/YYYY-MM-DD_HH-MM-SS_oom-events.txt`

## SSH Configuration

All scripts SSH to `root@atlas`. Ensure you have:

- SSH key authentication set up
- Host alias configured in `~/.ssh/config`:
  ```
  Host atlas
    HostName <atlas-ip>
    User root
  ```

Or modify the `HOST="root@atlas"` variable in each script.

## Interpreting Results

### Memory Thresholds

- **<80% used**: ✓ Normal
- **80-90% used**: ⚠️ Warning - getting tight
- **>90% used**: ⚠️ Critical - OOM imminent

### virtiofsd FD Counts

- **<1,000**: ✓ Normal
- **1,000-10,000**: ⚠️ Elevated - monitor closely
- **10,000-100,000**: ⚠️ High - likely growing
- **>100,000**: 🚨 Critical - memory leak active

### virtiofsd Memory Usage

- **<500 MB**: ✓ Normal
- **500 MB - 2 GB**: ⚠️ Elevated - monitor
- **2 GB - 5 GB**: ⚠️ High - leak likely
- **>5 GB**: 🚨 Critical - severe leak

## Troubleshooting

### "Permission denied" errors

Ensure SSH key authentication to root@atlas is working:

```bash
ssh root@atlas 'whoami'  # Should print: root
```

### "Command not found" errors

Scripts require: `bc`, `awk`, `grep`, `ps`, `free`
These should be installed by default on Proxmox.

### Scripts too slow

If scripts take too long, it might be due to:

- Many virtiofsd processes with huge FD counts
- Host under heavy load
- SSH connection latency

Consider running on the host directly instead of via SSH.

## Advanced Usage

### Run on host directly (faster)

```bash
# Copy scripts to atlas
scp -r ~/code/ducktape/wyrm-oom-investigation/scripts root@atlas:/tmp/

# SSH to atlas and run
ssh root@atlas
cd /tmp/scripts
./quick-check.sh
```

### Customize monitoring interval

Edit `monitor-continuous.sh`:

```bash
INTERVAL=60  # Change to desired seconds
```

### Add alerting

Wrap scripts in a cron job or systemd timer and send notifications:

```bash
# Example: Check every 5 minutes, alert if memory >90%
*/5 * * * * /path/to/quick-check.sh | grep "CRITICAL" && notify-send "OOM Warning"
```

## Next Steps After Running Scripts

Based on results:

1. **If virtiofsd has >100k FDs**:
   - VM likely not restarted after cache fix
   - Action: `ssh root@atlas 'qm shutdown 100 && qm start 100'`

2. **If no swap configured**:
   - Action: Follow swap setup in CLAUDE.md

3. **If memory consistently >90%**:
   - System structurally overcommitted
   - Action: Add RAM, reduce VM allocations, or migrate to NFS

4. **If cache=metadata not active**:
   - Config not applied or VM not restarted
   - Action: Apply config and restart VM

5. **If repeated OOM events**:
   - Chronic issue requiring architectural change
   - Action: Consider NFS migration (see CLAUDE.md)
