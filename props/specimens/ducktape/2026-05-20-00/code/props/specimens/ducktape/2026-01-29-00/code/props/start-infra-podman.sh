#!/bin/bash
set -euo pipefail

# Podman infrastructure startup script for props e2e testing
# Uses host networking (no Docker network isolation)
#
# CRITICAL: All podman run commands MUST include:
#   --annotation run.oci.keep_original_groups=1
# This bypasses /proc/self/setgroups which is unavailable in gVisor.
# Without this annotation, containers will fail to start.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="$SCRIPT_DIR/.devenv/state"
PASSWORD_FILE="$STATE_DIR/pg_password"

echo "=== Props Infrastructure Startup (Podman + Host Networking) ==="

# Generate PostgreSQL password if not exists
mkdir -p "$STATE_DIR"
if [[ ! -f "$PASSWORD_FILE" ]]; then
  echo "Generating PostgreSQL password..."
  openssl rand -base64 24 >"$PASSWORD_FILE"
  chmod 600 "$PASSWORD_FILE"
fi
PG_PASSWORD=$(cat "$PASSWORD_FILE")
echo "PostgreSQL password loaded from $PASSWORD_FILE"

# Stop any existing containers
echo "Cleaning up existing containers..."
podman rm -f props-postgres props-registry props-registry-proxy 2>/dev/null || true

# Start PostgreSQL (port 5433)
echo "Starting PostgreSQL on 127.0.0.1:5433..."
podman run -d --rm \
  --replace \
  --network=host \
  --annotation run.oci.keep_original_groups=1 \
  --name props-postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD="$PG_PASSWORD" \
  -e POSTGRES_DB=eval_results \
  -v props_eval_results_data:/var/lib/postgresql/data \
  docker.io/library/postgres:16 \
  postgres -c max_connections=200 -p 5433

# Wait for PostgreSQL
echo "Waiting for PostgreSQL to be ready..."
export PGPASSWORD="$PG_PASSWORD"
for i in {1..30}; do
  if psql -h 127.0.0.1 -p 5433 -U postgres -d postgres -c '\q' 2>/dev/null; then
    echo "PostgreSQL is ready"
    break
  fi
  if [ $i -eq 30 ]; then
    echo "ERROR: PostgreSQL failed to start within 30 seconds"
    exit 1
  fi
  sleep 1
done

# Start OCI Registry (port 5050)
echo "Starting OCI Registry on 127.0.0.1:5050..."
podman run -d --rm \
  --replace \
  --network=host \
  --annotation run.oci.keep_original_groups=1 \
  --name props-registry \
  -e REGISTRY_HTTP_ADDR=:5050 \
  -v props_registry_data:/var/lib/registry \
  docker.io/library/registry:2

# Wait for Registry
echo "Waiting for OCI Registry to be ready..."
for i in {1..30}; do
  if curl -sf http://127.0.0.1:5050/v2/ >/dev/null 2>&1; then
    echo "OCI Registry is ready"
    break
  fi
  if [ $i -eq 30 ]; then
    echo "ERROR: Registry failed to start within 30 seconds"
    exit 1
  fi
  sleep 1
done

# Build registry proxy image if needed
echo "Checking registry proxy image..."
if ! podman image inspect localhost/props-registry-proxy:latest >/dev/null 2>&1; then
  echo "Building registry proxy image..."
  cd "$SCRIPT_DIR/.." && bazelisk run //props/registry_proxy:load || {
    echo "ERROR: Failed to build proxy image"
    echo "  Try manually: cd $(pwd) && bazelisk run //props/registry_proxy:load"
    exit 1
  }
fi

# Start Registry Proxy (port 5051)
echo "Starting Registry Proxy on 127.0.0.1:5051..."
podman run -d --rm \
  --replace \
  --network=host \
  --annotation run.oci.keep_original_groups=1 \
  --name props-registry-proxy \
  -e PROPS_REGISTRY_UPSTREAM_URL=http://127.0.0.1:5050 \
  -e PGHOST=127.0.0.1 \
  -e PGPORT=5433 \
  -e PGUSER=postgres \
  -e PGPASSWORD="$PG_PASSWORD" \
  -e PGDATABASE=eval_results \
  localhost/props-registry-proxy:latest

# Wait for Proxy
echo "Waiting for Registry Proxy to be ready..."
for i in {1..30}; do
  # Proxy returns 401 for unauthenticated requests, which is expected
  if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5051/v2/ 2>/dev/null | grep -qE "^(200|401)$"; then
    echo "Registry Proxy is ready"
    break
  fi
  if [ $i -eq 30 ]; then
    echo "ERROR: Proxy failed to start within 30 seconds"
    echo "Check logs: podman logs props-registry-proxy"
    exit 1
  fi
  sleep 1
done

echo ""
echo "=== Infrastructure Ready ==="
echo "PostgreSQL:      127.0.0.1:5433 (user: postgres, password in $PASSWORD_FILE)"
echo "OCI Registry:    127.0.0.1:5050 (direct access)"
echo "Registry Proxy:  127.0.0.1:5051 (with ACL for agents)"
echo ""
echo "Environment variables (already set by session hook):"
echo "  PGHOST=127.0.0.1"
echo "  PGPORT=5433"
echo "  AGENT_PGHOST=127.0.0.1"
echo "  PROPS_REGISTRY_PROXY_HOST=127.0.0.1"
echo "  PROPS_REGISTRY_PROXY_PORT=5051"
echo "  PROPS_DOCKER_NETWORK=host"
echo ""
echo "To stop: podman stop props-postgres props-registry props-registry-proxy"
echo "To view logs: podman logs <container-name>"
echo ""
