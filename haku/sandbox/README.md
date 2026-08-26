# Haku Agent Sandbox client

The lifecycle client behind haku-console's `sandbox` MCP server: it creates
[Kubernetes SIG Agent Sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
`SandboxClaim` objects, waits for warm-pool adoption, bootstraps the assigned Pod,
runs bounded Bash commands through `pods/exec`, and disposes claims. It does not
install the controller, create templates or pools, or define RBAC.

The agent-facing tool surface lives in <../console/tools/sandbox.py>; this package
is the Kubernetes half it calls.

## Configuration

`SandboxEnvironmentConfig` (<config.py>) is Console's `agent_sandbox` block
(<../../cluster/k8s/haku/console/config.yaml>):

```yaml
agent_sandbox:
  sandbox:
    namespace: haku-sandbox
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
      /usr/local/bin/haku-sandbox-setup.sh
```

All durations are integer seconds. The initial TTL and bootstrap are fixed
deployment policy. Before every exec the client guarantees at least
`exec_ttl_extension_seconds` remain without shortening a later deadline.

`contract_hash` over that block identifies claims created under it, so an edit makes
live claims unusable until they are disposed — see <TODO.md>.

## External prerequisites

The deployment must supply the upstream `Sandbox`, `SandboxClaim`, and
`SandboxWarmPool` CRDs/controller, the configured warm pool and Pod container, and
permission for Console's ServiceAccount to manage claims, read Sandboxes/Pods, and
call `pods/exec` (<../../cluster/k8s/haku/workspaces/app/haku-console-sandbox-role.yaml>).
Those resources intentionally remain outside this package.
