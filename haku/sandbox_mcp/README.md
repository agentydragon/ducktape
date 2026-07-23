# Haku Agent Sandbox MCP

Standalone FastMCP service for allocating and using one preconfigured
[Kubernetes SIG Agent Sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
environment. The server creates `SandboxClaim` objects, waits for warm-pool
adoption, bootstraps the assigned Pod, runs bounded Bash commands through
`pods/exec`, and disposes claims. It does not install the controller, create
templates or pools, or define RBAC.

## Configuration

Set `HAKU_SANDBOX_MCP_CONFIG_FILE` to a non-secret YAML file:

```yaml
sandbox:
  namespace: agent-workspaces
  warm_pool: haku
  container: workspace
  default_cwd: /workspace/haku-state
  initial_ttl_seconds: 28800
  exec_ttl_extension_seconds: 7200
  provisioning_timeout_seconds: 600
  max_exec_timeout_seconds: 300
  max_output_bytes: 100000

bootstrap:
  cwd: /workspace
  timeout_seconds: 300
  script: |
    set -euo pipefail
    repo=/workspace/haku-state
    url=http://forgejo-http.forgejo:3000/haku/haku-state.git
    if [[ -d "$repo/.git" ]]; then
      git -C "$repo" fetch --prune origin master
      git -C "$repo" checkout -B master origin/master
      git -C "$repo" reset --hard origin/master
    else
      git clone --branch master --single-branch "$url" "$repo"
    fi
```

Set `HAKU_SANDBOX_MCP_BEARER_TOKEN` from a Secret. Optional process settings
are `HAKU_SANDBOX_MCP_HOST` and `HAKU_SANDBOX_MCP_PORT`. The service loads its
Kubernetes identity from the mounted in-cluster ServiceAccount token; it never
names or discovers RBAC objects.

All durations are integer seconds. The initial TTL and bootstrap are fixed
deployment policy. Before every exec, the server guarantees at least
`exec_ttl_extension_seconds` remain without shortening a later deadline.

## External prerequisites

The deployment must supply the upstream `Sandbox`, `SandboxClaim`, and
`SandboxWarmPool` CRDs/controller, the configured warm pool and Pod container,
and permission to manage claims, read Sandboxes/Pods, and call `pods/exec`.
Those resources intentionally remain outside this package.
