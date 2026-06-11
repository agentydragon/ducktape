# Container Death Debugging Guide

## The Problem

Containers are dying between setup completion and global user pre-task script execution. We get "container is not running" errors when trying to docker exec.

## Debugging Layers

### 1. Real-Time Monitoring (Live Required)

#### Docker Events Stream

```bash
# Terminal 1: Monitor all Docker events
docker events --format 'table {{.Time}}\t{{.Status}}\t{{.ID}}\t{{.Image}}' > docker_events.log &

# Terminal 2: Filter for container lifecycle
docker events --filter type=container --format '{{.Time}} {{.Status}} {{.Actor.Attributes.name}} {{.Actor.ID}}' > container_lifecycle.log &
```

#### Colima VM Logs (Live)

```bash
# Terminal 3: Colima VM kernel messages
colima ssh -- sudo journalctl -f > colima_kernel.log &

# Terminal 4: Docker daemon logs in VM
colima ssh -- sudo journalctl -u docker -f > docker_daemon.log &
```

#### System Resource Monitoring

```bash
# Terminal 5: Real-time resource monitoring
watch -n 0.5 'echo "=== DOCKER STATS ==="; docker stats --no-stream; echo "=== VM MEMORY ==="; colima ssh -- free -h; echo "=== VM PROCESSES ==="; colima ssh -- ps aux | wc -l' > resource_monitor.log &
```

### 2. System Call Tracing (In Colima VM)

#### Wrap pre-task global user script with strace

#### Full Process Tree Tracing

```bash
# In Colima VM, trace the entire process tree
colima ssh -- sudo strace -f -e trace=execve,kill,exit,clone -o /tmp/full_trace.log -p $(pgrep dockerd) &
```

### 3. Container Lifecycle Deep Inspection

#### Enhanced Container Status Checking

```bash
# Add to setup script before EVERY docker exec:
check_container_status() {
    local container_id=$1
    echo "[DEBUG] Checking container $container_id at $(date)"
    docker inspect "$container_id" --format '{{.State.Status}} {{.State.Running}} {{.State.ExitCode}} {{.State.Error}}' || echo "INSPECT FAILED"
    docker ps -a --filter id="$container_id" --format 'table {{.ID}}\t{{.Status}}\t{{.Image}}' || echo "PS FAILED"
}
```

#### Container Health Monitoring

```bash
# Monitor container health continuously
monitor_container() {
    local container_id=$1
    while true; do
        status=$(docker inspect "$container_id" --format '{{.State.Status}}' 2>/dev/null || echo "GONE")
        echo "$(date): Container $container_id status: $status"
        if [ "$status" != "running" ]; then
            echo "CONTAINER DIED: $status"
            docker logs "$container_id" 2>&1 | tail -20
            break
        fi
        sleep 0.1
    done
}
```

### 4. Resource Limit Investigation

#### Increase Colima Resources

```bash
# Stop and restart with more resources
colima stop
colima start --cpu 8 --memory 16 --disk 200
```

#### Docker Resource Monitoring

```bash
# Check if containers have resource limits
inspect_container_limits() {
    local container_id=$1
    docker inspect "$container_id" | jq '{
        Memory: .HostConfig.Memory,
        CpuShares: .HostConfig.CpuShares,
        PidsLimit: .HostConfig.PidsLimit,
        OomKillDisable: .HostConfig.OomKillDisable,
        State: .State
    }'
}
```

### 5. Post-Mortem Analysis (Queryable)

#### Docker Logs

```bash
# Collect all container logs
docker logs container_id > container_death.log 2>&1

# Docker daemon logs from Colima
colima ssh -- sudo journalctl -u docker --since "10 minutes ago" > docker_daemon_postmortem.log
```

#### System Event Logs

```bash
# macOS system logs
log show --last 10m --predicate 'subsystem contains "docker" or subsystem contains "colima"' > macos_system.log

# Colima VM system logs
colima ssh -- sudo journalctl --since "10 minutes ago" > vm_system.log
```

#### OOM Analysis

```bash
# Check for OOM kills in VM
colima ssh -- sudo dmesg | grep -i "killed\|oom\|memory" > oom_analysis.log

# Memory pressure analysis
colima ssh -- cat /proc/meminfo > meminfo_snapshot.log
colima ssh -- cat /proc/pressure/memory > memory_pressure.log 2>/dev/null || echo "No PSI support"
```

## Implementation Strategy

### Step 1: Wrap script with Full Monitoring

```bash
#!/bin/bash
# Enhanced script with full debugging

# Start background monitoring
monitor_container "$CONTAINER_ID" > "/tmp/container_monitor_$CONTAINER_ID.log" &
MONITOR_PID=$!

# Wrap every docker command with checks
docker_exec_safe() {
    local container_id=$1
    shift

    echo "[DEBUG] Before docker exec: $(date)"
    docker inspect "$container_id" --format '{{.State.Status}} {{.State.Running}}' || {
        echo "FATAL: Container inspection failed before docker exec"
        return 1
    }

    strace -f -o "/tmp/docker_exec_trace_$(date +%s).log" docker exec "$container_id" "$@"
    local exit_code=$?

    echo "[DEBUG] After docker exec: $(date), exit code: $exit_code"
    return $exit_code
}

# Replace all "docker exec" calls with "docker_exec_safe"
```

### Step 2: Enable All Logging Streams

```bash
# Start all monitoring in background
start_full_monitoring() {
    docker events --format '{{.Time}} {{.Status}} {{.Actor.ID}}' > docker_events.log &
    colima ssh -- sudo journalctl -u docker -f > docker_daemon.log &
    watch -n 0.1 'docker stats --no-stream' > docker_stats.log &
}
```

### Step 3: Container Death Detection

```bash
# Detect exact moment of container death
detect_container_death() {
    local container_id=$1

    # Poll container status rapidly
    while docker inspect "$container_id" >/dev/null 2>&1; do
        sleep 0.01  # 10ms polling
    done

    echo "CONTAINER DEATH DETECTED: $(date)"

    # Immediate post-mortem
    docker ps -a --filter id="$container_id" --format 'table {{.Status}}\t{{.Image}}'
    docker logs "$container_id" 2>&1 | tail -50

    # System state snapshot
    colima ssh -- sudo dmesg | tail -20
    colima ssh -- free -h
    docker system df
}
```

## Root Cause Hypotheses (Priority Order)

1. **Container Resource Exhaustion**: Container hits memory/CPU limits and gets killed
2. **Docker Daemon Issues**: dockerd crashes or restarts, killing containers
3. **Colima VM Resource Pressure**: VM runs out of resources, kernel kills processes
4. **Container Process Exit**: Main container process exits, stopping container
5. **Network/Storage Issues**: Container loses access to mounted volumes/networks
6. **Race Condition**: Container setup race between remounting and script execution

## Action Plan

1. **Immediate**: Implement container death detection wrapper
2. **Short-term**: Add strace to all docker commands
3. **Medium-term**: Increase Colima resources and monitor
4. **Debug**: Analyze all log files post-failure to identify pattern

The key insight: **We need to catch the container dying in real-time, not just discover it died.**
