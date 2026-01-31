#!/bin/bash
# Minimal probe: can we talk to a Docker daemon on this RBE worker?
set -euo pipefail

echo "=== Environment ==="
echo "Hostname: $(hostname)"
echo "Kernel: $(uname -r)"
echo "User: $(whoami)"
echo "Docker socket: $(ls -la /var/run/docker.sock 2>&1 || echo 'not found')"
echo "Memory: $(free -h 2>/dev/null | head -2 || echo 'free not available')"
echo "CPUs: $(nproc 2>/dev/null || echo 'nproc not available')"
echo "Disk: $(df -h / 2>/dev/null | tail -1 || echo 'df not available')"

echo ""
echo "=== Processes ==="
ps aux 2>/dev/null || echo "ps not available"

echo ""
echo "=== Docker info ==="
docker info 2>&1 || echo "docker info failed: $?"

echo ""
echo "=== Docker run ==="
docker run --rm alpine:3.20 echo "DOCKER_WORKS" 2>&1
