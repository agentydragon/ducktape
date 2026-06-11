# E2E Tests in Claude Code Environment

This document outlines the plan for running props e2e tests in the Claude Code
(web) environment, which uses gVisor sandbox with host networking.

## Current State

### What Exists

1. **`props/start-infra-podman.sh`** - Partial implementation that starts:
   - PostgreSQL on port 5433
   - OCI Registry on port 5050
   - Registry Proxy on port 5051

2. **`PROPS_E2E_HOST_HOSTNAME` env var** - Configures hostname for containers
   to reach host services (set to `127.0.0.1` for host networking)

3. **Session start hook** - Installs podman with host networking support

4. **`.bazelrc` config** - Default `test` stanza passes through all required
   environment variables when set in the environment

### What's Missing

1. Database schema setup (recreate command)
2. Building and pushing agent images to local registry
3. Session hook integration to trigger setup via env var
4. Podman socket compatibility verification

## Environment Constraints

Claude Code runs in gVisor sandbox with these constraints:

| Constraint                | Impact                               | Solution                                          |
| ------------------------- | ------------------------------------ | ------------------------------------------------- |
| No overlay filesystem     | Can't use overlayfs storage          | Use `vfs` storage driver                          |
| No Docker socket          | Can't use Docker                     | Use Podman                                        |
| No bridge networking      | Containers can't use Docker networks | Use `--network=host`                              |
| No `/proc/self/setgroups` | Container startup fails              | Add `--annotation run.oci.keep_original_groups=1` |
| TLS-inspecting proxy      | Registry pulls may fail              | Use bazel proxy (already configured)              |

## Required Environment Variables

For e2e tests to run, these must be set and exported to `CLAUDE_ENV_FILE`:

```bash
# Database connection (host process)
PGHOST=127.0.0.1
PGPORT=5433
PGUSER=postgres
PGPASSWORD=<generated>
PGDATABASE=eval_results

# Agent container configuration
AGENT_PGHOST=127.0.0.1           # Host from container's perspective
PROPS_REGISTRY_PROXY_HOST=127.0.0.1
PROPS_REGISTRY_PROXY_PORT=5051
PROPS_DOCKER_NETWORK=host        # Use host networking for agents
PROPS_E2E_HOST_HOSTNAME=127.0.0.1  # For test fixtures
DOCKER_HOST=unix:///run/podman/podman.sock  # For podman compatibility
```

These are configured in `.bazelrc` default `test` stanza for automatic
passthrough when set in the environment.

## Implementation Plan

### Phase 1: Session Hook Integration

Update session start hook to trigger props infrastructure when enabled.
**Important**: The session hook should NOT run any bazel builds (they're slow).

1. **Trigger via environment variable**: Set `SESSION_HOOK_PROPS_SETUP=1` in
   Claude Code web environment picker to enable props e2e setup

2. **When triggered, the session hook will**:
   - Start podman system service (already done)
   - Run `props/start-infra-podman.sh` to start containers only (no bazel)
   - Export all required env vars to `CLAUDE_ENV_FILE`

3. **Manual setup steps** (run after session starts):

   ```bash
   # Recreate database with schema and sync data
   bazel run //props/cli:cli -- db recreate -y

   # Build and push agent images to local registry
   bazel run //props/critic:push
   bazel run //props/grader:push
   bazel run //props/critic_dev/improve:push
   bazel run //props/critic_dev/optimize:push
   ```

4. **Running tests**:

   ```bash
   bazel test //props/critic:test_e2e
   ```

   The `.bazelrc` default `test` stanza automatically passes through env vars when set.

### Phase 2: AgentRegistry Podman Support

The `AgentRegistry` class currently uses `aiodocker`. For Claude Code, we will use
**podman's Docker-compatible API**:

- Start podman with `podman system service`
- Point `DOCKER_HOST` to podman socket
- `aiodocker` should work as-is with the Docker-compatible socket

If aiodocker doesn't support required annotations, we may need a podman-specific
fallback using subprocess with `--annotation run.oci.keep_original_groups=1`.

## Detailed Task Breakdown

### Task 1: Update Session Hook

Add props setup logic to the session start hook. **No bazel builds** - only start
containers and export env vars:

```python
# In session_start.py

if os.environ.get("SESSION_HOOK_PROPS_SETUP") == "1":
    # Run props infrastructure setup (containers only, no bazel)
    subprocess.run(
        ["bash", "props/start-infra-podman.sh"],
        cwd=project_dir,
        check=True,
    )

    # Export environment variables to CLAUDE_ENV_FILE
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if env_file:
        with open(env_file, "a") as f:
            f.write("export PGHOST=127.0.0.1\n")
            f.write("export PGPORT=5433\n")
            f.write("export PGUSER=postgres\n")
            f.write(f"export PGPASSWORD={pg_password}\n")
            f.write("export PGDATABASE=eval_results\n")
            f.write("export AGENT_PGHOST=127.0.0.1\n")
            f.write("export PROPS_REGISTRY_PROXY_HOST=127.0.0.1\n")
            f.write("export PROPS_REGISTRY_PROXY_PORT=5051\n")
            f.write("export PROPS_DOCKER_NETWORK=host\n")
            f.write("export PROPS_E2E_HOST_HOSTNAME=127.0.0.1\n")
            f.write("export DOCKER_HOST=unix:///run/podman/podman.sock\n")
```

The `start-infra-podman.sh` script should only start containers (postgres, registry,
registry-proxy). Database setup and image builds are done manually after session starts.

### Task 2: Verify aiodocker with Podman

Test that `aiodocker` works with podman's Docker-compatible socket:

```python
import aiodocker
import os

os.environ["DOCKER_HOST"] = "unix:///run/podman/podman.sock"
docker = aiodocker.Docker()
# Test basic operations: list images, create container, etc.
```

If this works, no changes to AgentRegistry needed.

### Task 3: Handle gVisor Annotations (if needed)

If aiodocker doesn't support annotations, we need to:

1. Create a podman-specific container runner
2. Use subprocess to call `podman run` directly
3. Add `--annotation run.oci.keep_original_groups=1` to all runs

## Usage

### Claude Code on the Web

1. Set `SESSION_HOOK_PROPS_SETUP=1` in environment picker when starting session
2. Wait for infrastructure containers to start (shown in session hook output)
3. Run manual setup (one-time per session):
   ```bash
   bazel run //props/cli:cli -- db recreate -y
   bazel run //props/critic:push
   bazel run //props/grader:push
   bazel run //props/critic_dev/improve:push
   bazel run //props/critic_dev/optimize:push
   ```
4. Run tests:
   ```bash
   bazel test //props/critic:test_e2e
   ```

### Local Development

1. Start infrastructure: `docker compose -f props/compose.yaml up -d`
2. Set environment variables (or source from devenv)
3. Run setup: `bazel run //props/cli:cli -- db recreate -y` and image pushes
4. Run tests:
   ```bash
   bazel test //props/critic:test_e2e
   ```

## Incremental Validation

| Step | Command                                 | Validates                |
| ---- | --------------------------------------- | ------------------------ |
| 1    | `podman ps`                             | Containers running       |
| 2    | `curl http://127.0.0.1:5050/v2/`        | Registry accessible      |
| 3    | `psql -h 127.0.0.1 -p 5433 -U postgres` | PostgreSQL accessible    |
| 4    | `bazel test //props/db:test_session`    | DB connection from Bazel |
| 5    | `bazel test //props/critic:test_e2e`    | Full e2e stack           |

## Open Questions

1. **Podman + aiodocker compatibility**: Does aiodocker work with podman socket?
   - If not, need subprocess-based approach

2. **Image pull through proxy**: Can podman pull base images through the TLS proxy?
   - May need to pre-pull images in session hook

3. **Persistent volumes**: Do podman volumes persist across sessions?
   - If not, database needs re-initialization each session

4. **Timeout**: E2E tests can take 3-5 minutes each
   - May hit Claude Code session timeouts

## Next Steps

1. [ ] Test podman socket with aiodocker locally
2. [ ] Add SESSION_HOOK_PROPS_SETUP handling to session hook (containers + env vars only)
3. [ ] Test in Claude Code environment
