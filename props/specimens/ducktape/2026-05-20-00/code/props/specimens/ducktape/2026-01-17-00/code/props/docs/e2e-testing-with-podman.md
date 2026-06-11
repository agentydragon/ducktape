# E2E Testing with Podman (Host Networking)

## Overview

Proposal for adapting the props system to run e2e tests in environments with:

- **Podman** instead of Docker
- **VFS storage driver** (no overlay filesystem)
- **Host networking only** (no network isolation)

This enables testing in gVisor sandboxes (Claude Code web environment) without full Docker network capabilities.

## Current Architecture vs Adapted Architecture

### Current (Docker with Network Isolation)

Two isolated Docker networks enforce access boundaries:

- **props-internal network**: PostgreSQL (:5432), Registry (:5000), Proxy (:5051) - infrastructure services
- **props-agents network**: Agent containers plus proxied access to PostgreSQL and Proxy

Network isolation prevents agents from reaching Registry:5000 directly - they must use Proxy:5051 which enforces ACL based on agent type.

### Adapted (Podman with Host Networking)

All services run on host network (127.0.0.1) with distinct ports:

- PostgreSQL: 127.0.0.1:5433
- Registry: 127.0.0.1:5050
- Proxy: 127.0.0.1:5051
- Agent containers: host network mode, can reach all services via localhost

Network isolation not enforced. Tests verify proxy ACL logic works correctly but don't prevent agents from bypassing the proxy.

## Implementation Strategy

### 1. Infrastructure Services (podman containers)

All services run with `--network=host`:

```bash
# PostgreSQL
podman run --rm --network=host \
  --name props-postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD="$PG_PASSWORD" \
  -e POSTGRES_DB=eval_results \
  -v props_eval_results_data:/var/lib/postgresql/data \
  postgres:16 \
  -c max_connections=200 \
  -p 5433

# OCI Registry
podman run --rm --network=host \
  --name props-registry \
  -e REGISTRY_HTTP_ADDR=:5050 \
  -v props_registry_data:/var/lib/registry \
  registry:2

# Registry Proxy
podman run --rm --network=host \
  --name props-registry-proxy \
  -e PROPS_REGISTRY_UPSTREAM_URL=http://127.0.0.1:5050 \
  -e PGHOST=127.0.0.1 -e PGPORT=5433 \
  -e PGUSER=postgres -e PGPASSWORD="$PG_PASSWORD" \
  -e PGDATABASE=eval_results \
  props-registry-proxy:latest
```

**Key changes:**

- All use `--network=host`
- Services bind to specific ports on 127.0.0.1
- Use localhost addresses instead of container names

### 2. Agent Containers

Agents also use host networking:

```python
# In agent_setup.py or equivalent
container = await docker_client.containers.create(
    config={
        "Image": image,
        "Env": [
            # Use localhost for all services
            "PGHOST=127.0.0.1",
            "PGPORT=5433",
            "PGUSER=agent_{run_id}",
            "PGPASSWORD={password}",
            "MCP_SERVER_URL=http://127.0.0.1:{mcp_port}",
            "MCP_SERVER_TOKEN={token}",
        ],
        "HostConfig": {
            "NetworkMode": "host",  # Instead of "props-agents"
            "StorageOpt": {
                "driver": "vfs"  # Required for gVisor
            }
        },
        "WorkingDir": "/workspace",
    }
)
```

**Registry access for agent-author agents:**

```python
# In agent container, agents use localhost
registry_url = "http://127.0.0.1:5051"  # Proxy port
auth = (username, password)
```

### 3. Code Changes Required

#### a. Use Environment Variables for All Configuration

All configuration comes from environment variables - no Python-level detection or branching:

```python
# props/core/agent_setup.py
import os

class AgentEnvironment(ABC):
    def __init__(self, ...):
        # Read from environment (set by devenv or Claude Code hook)
        # Agent container's postgres host (differs between Docker/podman)
        self._agent_pghost = os.environ.get("AGENT_PGHOST", "props-postgres")
        # Port is same for both host and agents
        self._pgport = os.environ.get("PGPORT", "5432")
        self._registry_proxy_host = os.environ.get("PROPS_REGISTRY_PROXY_HOST", "registry-proxy")
        self._registry_proxy_port = os.environ.get("PROPS_REGISTRY_PROXY_PORT", "5050")
        self._docker_network = os.environ.get("PROPS_DOCKER_NETWORK", "props-agents")
        ...

    async def _create_container(self, ...):
        config = {
            "Image": self._image,
            "Env": [
                f"PGHOST={self._agent_pghost}",  # Agent's postgres host
                f"PGPORT={self._pgport}",  # Same port for all
                f"MCP_SERVER_URL=http://{mcp_host}:{mcp_port}",
                ...
            ],
            "HostConfig": {
                "NetworkMode": self._docker_network,  # "host" or "props-agents"
            }
        }

        return await self._docker_client.containers.create(config=config)
```

**Note**: Storage driver (VFS vs overlay) is configured at the podman level in `~/.config/containers/storage.conf`, not per-container.

#### b. Docker Client Socket Detection

Detect podman vs Docker socket automatically:

```python
# mcp_infra/docker/client.py (or similar)
import os
from pathlib import Path
import aiodocker

def get_docker_socket() -> str:
    """Get Docker/Podman socket path."""
    # Explicit override
    if socket := os.environ.get("DOCKER_HOST"):
        return socket

    # Check for podman socket (rootless)
    podman_socket = Path("/run/user") / str(os.getuid()) / "podman/podman.sock"
    if podman_socket.exists():
        return f"unix://{podman_socket}"

    # Check for Docker socket
    docker_socket = Path("/var/run/docker.sock")
    if docker_socket.exists():
        return f"unix://{docker_socket}"

    raise RuntimeError("Neither Docker nor Podman socket found")
```

#### c. Registry Client Configuration

Registry client reads proxy address from environment:

```python
# props/core/oci_utils.py
import os

REGISTRY_PROXY_HOST = os.environ.get("PROPS_REGISTRY_PROXY_HOST", "registry-proxy")
REGISTRY_PROXY_PORT = os.environ.get("PROPS_REGISTRY_PROXY_PORT", "5050")

def build_oci_reference(agent_type: AgentType, digest: str) -> str:
    """Build full OCI reference from agent type and digest."""
    repository = str(agent_type)
    return f"{REGISTRY_PROXY_HOST}:{REGISTRY_PROXY_PORT}/{repository}@{digest}"
```

### 4. Bazel Test Environment Variables

Tests run with `bazel test`, which needs environment variables configured:

```python
# props/core/BUILD.bazel (or test-specific BUILD files)
py_test(
    name = "test_e2e_critic",
    srcs = ["tests/critic/test_e2e.py"],
    env = {
        # Inherit from parent environment (set by devenv or CI)
        "PGHOST": "$(PGHOST)",  # Host-side postgres access
        "PGPORT": "$(PGPORT)",  # Same port for both host and agents
        "AGENT_PGHOST": "$(AGENT_PGHOST)",  # Agent container's postgres host
        "PROPS_REGISTRY_PROXY_HOST": "$(PROPS_REGISTRY_PROXY_HOST)",
        "PROPS_REGISTRY_PROXY_PORT": "$(PROPS_REGISTRY_PROXY_PORT)",
        "PROPS_DOCKER_NETWORK": "$(PROPS_DOCKER_NETWORK)",
    },
    deps = [...],
)
```

**Alternative: Use `--test_env` flag:**

```bash
# Pass environment explicitly to all tests
bazel test //props/... \
  --test_env=PGHOST=127.0.0.1 \
  --test_env=PGPORT=5433 \
  --test_env=AGENT_PGHOST=127.0.0.1 \
  --test_env=PROPS_DOCKER_NETWORK=host
```

**Or: Use `.bazelrc` for consistent configuration:**

```bash
# .bazelrc
test:podman --test_env=PGHOST=127.0.0.1
test:podman --test_env=PGPORT=5433
test:podman --test_env=AGENT_PGHOST=127.0.0.1
test:podman --test_env=PROPS_REGISTRY_PROXY_HOST=127.0.0.1
test:podman --test_env=PROPS_REGISTRY_PROXY_PORT=5051
test:podman --test_env=PROPS_DOCKER_NETWORK=host

# Then run: bazel test --config=podman //props/...
```

### 5. DevEnv Configuration

#### a. Docker Mode (devenv.nix - current)

Current devenv sets environment for Docker + network isolation:

```nix
# props/devenv.nix
{
  env = {
    # PostgreSQL client variables (host-side access)
    PGHOST = "127.0.0.1";
    PGPORT = "5433";
    PGUSER = "postgres";
    PGDATABASE = "eval_results";

    # Agent container configuration
    AGENT_PGHOST = "props-postgres";  # Container name (differs from PGHOST)
    # PGPORT = "5433" (same port for both host and agents)

    # Registry proxy
    PROPS_REGISTRY_PROXY_HOST = "registry-proxy";
    PROPS_REGISTRY_PROXY_PORT = "5050";

    # Docker networking
    PROPS_DOCKER_NETWORK = "props-agents";
  };
}
```

**Note**: Docker uses overlay storage driver by default (no configuration needed).

#### b. Claude Code Web Hook (Podman Mode)

Claude Code web startup hook configures podman and sets environment variables:

```python
# claude_web_hooks/session_start.py additions
import subprocess
from pathlib import Path

def setup_podman_storage():
    """Configure podman to use VFS storage driver (required for gVisor)."""
    storage_conf = Path.home() / ".config/containers/storage.conf"
    storage_conf.parent.mkdir(parents=True, exist_ok=True)

    # Write minimal storage.conf with VFS driver
    storage_conf.write_text("""[storage]
driver = "vfs"
""")

    log.info("Configured podman storage driver: vfs")

def setup_props_environment():
    """Set environment variables for props testing with podman."""
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file:
        return

    # Podman + host networking configuration
    env_content = """
# Props e2e test configuration (podman + host networking)
export PGHOST=127.0.0.1
export PGPORT=5433
export AGENT_PGHOST=127.0.0.1  # Same as PGHOST in host networking mode
export PROPS_REGISTRY_PROXY_HOST=127.0.0.1
export PROPS_REGISTRY_PROXY_PORT=5051
export PROPS_DOCKER_NETWORK=host
"""

    with open(env_file, "a") as f:
        f.write(env_content)

# Call from main()
if project_dir_str and (Path(project_dir_str) / "props").is_dir():
    setup_podman_storage()
    setup_props_environment()
```

**Note**: Podman storage driver must be configured before creating any containers. The hook writes `~/.config/containers/storage.conf` with `driver = "vfs"` for gVisor compatibility.

#### c. Startup Script for Podman Infrastructure

```bash
# props/devenv-podman.sh
#!/bin/bash
set -euo pipefail

# Generate password
mkdir -p .devenv/state
if [ ! -f .devenv/state/pg_password ]; then
    openssl rand -base64 32 > .devenv/state/pg_password
fi
PG_PASSWORD=$(cat .devenv/state/pg_password)

# Start PostgreSQL
podman run --rm -d --network=host \
  --name props-postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD="$PG_PASSWORD" \
  -e POSTGRES_DB=eval_results \
  -v props_eval_results_data:/var/lib/postgresql/data \
  postgres:16 \
  postgres -c max_connections=200 -p 5433

# Wait for postgres
until PGPASSWORD="$PG_PASSWORD" psql -h 127.0.0.1 -p 5433 -U postgres -d postgres -c '\q' 2>/dev/null; do
  echo "Waiting for postgres..."
  sleep 1
done

# Start Registry
podman run --rm -d --network=host \
  --name props-registry \
  -e REGISTRY_HTTP_ADDR=:5050 \
  -v props_registry_data:/var/lib/registry \
  registry:2

# Wait for registry
until curl -sf http://127.0.0.1:5050/v2/ >/dev/null 2>&1; do
  echo "Waiting for registry..."
  sleep 1
done

# Build proxy image if needed
if ! podman image inspect props-registry-proxy:latest >/dev/null 2>&1; then
  echo "Building proxy image..."
  bazelisk run //props/registry_proxy:load
fi

# Start Proxy
podman run --rm -d --network=host \
  --name props-registry-proxy \
  -e PROPS_REGISTRY_UPSTREAM_URL=http://127.0.0.1:5050 \
  -e PGHOST=127.0.0.1 -e PGPORT=5433 \
  -e PGUSER=postgres -e PGPASSWORD="$PG_PASSWORD" \
  -e PGDATABASE=eval_results \
  props-registry-proxy:latest

# Wait for proxy
until curl -sf http://127.0.0.1:5051/v2/ >/dev/null 2>&1; do
  echo "Waiting for proxy..."
  sleep 1
done

echo "Infrastructure ready!"
echo "PostgreSQL: 127.0.0.1:5433"
echo "Registry: 127.0.0.1:5050"
echo "Proxy: 127.0.0.1:5051"
echo ""
echo "Environment variables have been set by Claude Code web hook"
```

### 6. Running Tests

#### With Bazel (Docker + devenv)

```bash
# Start infrastructure with devenv
cd props
devenv up

# Run all e2e tests (uses env vars from devenv.nix)
bazel test //props/core/...

# Run specific test
bazel test //props/core/critic:test_e2e
```

#### With Bazel (Podman + Claude Code web)

```bash
# Start infrastructure (one-time setup)
./props/devenv-podman.sh &

# Environment variables already set by Claude Code web hook
# Just run tests directly
bazel test //props/core/...

# Or use explicit config
bazel test --config=podman //props/core/...
```

#### With pytest (if needed for debugging)

```bash
# Bazel runs pytest internally, but you can also run pytest directly
cd props

# Docker mode
pytest core/critic/test_e2e.py -m requires_docker

# Podman mode (env vars from hook)
pytest core/critic/test_e2e.py -m requires_docker
```

## Trade-offs

### What We Lose

1. **Network isolation enforcement**: Agents can technically reach registry:5050 directly
2. **Port conflicts**: All services must use unique ports on host
3. **Service discovery**: Must use localhost addresses instead of container names

### What We Keep

1. **Functional correctness**: Tests verify proxy ACL logic works
2. **Agent capabilities**: All agent workflows function correctly
3. **Database isolation**: RLS policies still enforce data scoping
4. **End-to-end validation**: Full stack testing from agent launch to results

### What We Gain

1. **Podman compatibility**: Works in rootless podman environments
2. **gVisor compatibility**: VFS storage + host networking work in sandboxes
3. **Simplified networking**: No need to manage Docker networks
4. **Faster iteration**: Easier to debug (all services on localhost)

## Security Considerations

**For E2E testing (acceptable):**

- Network isolation not enforced, but proxy ACL logic is tested
- Tests verify the proxy correctly validates credentials and enforces permissions
- Tests verify agents can/cannot perform operations based on their type

**For production (not acceptable):**

- Network isolation is a critical security boundary
- Production deployments should use Docker with network isolation
- This podman setup is **development/testing only**

## Migration Path

1. **Phase 1**: Add runtime config abstraction (no behavior change)
2. **Phase 2**: Test with Docker + host networking (verify compatibility)
3. **Phase 3**: Test with podman + host networking (Claude Code web env)
4. **Phase 4**: Update CI to test both modes

## Testing Strategy

Tests verify **functional correctness**, not **security enforcement**:

```python
# Example: Test proxy ACL allows/denies correctly
async def test_proxy_acl_allows_agent_author_push():
    """Verify proxy allows agent authors to push by digest."""
    # Agent author credentials
    response = httpx.put(
        f"http://127.0.0.1:5051/v2/critic/manifests/{digest}",
        auth=(agent_username, agent_password),
        content=manifest_json
    )
    assert response.status_code == 201  # Allowed

async def test_proxy_acl_denies_critic_push():
    """Verify proxy denies critic agents push access."""
    # Critic credentials
    response = httpx.put(
        f"http://127.0.0.1:5051/v2/critic/manifests/{digest}",
        auth=(critic_username, critic_password),
        content=manifest_json
    )
    assert response.status_code == 403  # Forbidden
```

Tests don't verify that critics **cannot bypass** the proxy (network isolation), just that the proxy **correctly enforces** ACL when used.

## Environment Variables Summary

All configuration is controlled via environment variables (no Python-level branching):

| Variable                    | Docker Mode                  | Podman Mode | Purpose                                                 |
| --------------------------- | ---------------------------- | ----------- | ------------------------------------------------------- |
| `PGHOST`                    | `127.0.0.1`                  | `127.0.0.1` | Host-side postgres access (both modes)                  |
| `PGPORT`                    | `5433`                       | `5433`      | Postgres port (same for host and agents)                |
| `AGENT_PGHOST`              | `props-postgres` (container) | `127.0.0.1` | Postgres host for agent containers (differs from PGHOST |
| `PROPS_REGISTRY_PROXY_HOST` | `registry-proxy` (container) | `127.0.0.1` | Registry proxy host for agents                          |
| `PROPS_REGISTRY_PROXY_PORT` | `5050`                       | `5051`      | Registry proxy port for agents                          |
| `PROPS_DOCKER_NETWORK`      | `props-agents`               | `host`      | Docker network mode for agent containers                |
| `DOCKER_HOST`               | (auto)                       | (auto)      | Docker/Podman socket path (auto-detected)               |

**Key insights**:

- Only `AGENT_PGHOST` differs between Docker and podman modes (container name vs localhost)
- The port (`PGPORT`) is the same for both host-side and agent-side access
- Storage driver (VFS vs overlay) is configured at the podman/Docker level, not via environment variables

**Configuration Sources:**

- **Docker mode**: Environment variables set by `props/devenv.nix`
- **Podman mode**: Environment variables set by Claude Code web hook; storage driver configured in `~/.config/containers/storage.conf`
- **Tests**: Inherit from environment (via Bazel `--test_env` or `.bazelrc`)

## Summary

This proposal enables e2e testing in both Docker and podman environments by:

1. **Environment variable configuration** - All hosts/ports configurable via env vars
2. **No Python-level branching** - Code reads from environment, works in both modes
3. **Bazel test integration** - Tests inherit environment via `--test_env` flags
4. **Claude Code web hook** - Sets podman-specific env vars automatically
5. **DevEnv compatibility** - Existing Docker setup continues working unchanged

**Key insight**: Same Python code works in both modes because all configuration comes from environment variables. No need for separate fixtures, RuntimeConfig classes, or conditional logic.

**Result**: Full e2e test coverage in Claude Code web environment (podman + host networking) and production environment (Docker + network isolation) using the same test code.
