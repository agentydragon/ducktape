# Parked Haku Console Sandbox MCP

This is the retired Haku Console in-process `sandbox` MCP server and its Kubernetes client. It
created `SandboxClaim` objects, waited for warm-pool adoption, bootstrapped the assigned Pod, ran
bounded commands through `pods/exec`, and disposed claims.

The server was removed from the deployed catalog in PR #8620; the agent-facing `/mcp` endpoint was
disabled in PR #8621. This code and its Bazel tests are retained here as reference material and are
not linked into the Console application. The Haku-specific `SandboxTemplate`, `SandboxWarmPool`,
janitor, and Console Role/RoleBinding were removed from active Flux output. The warm-pool feature is
not planned for Agentplane.

The generic Agent Sandbox cdk8s support remains in use by other workspaces. The old image and
bootstrap sources are in [`image/`](image/), with their former rendered resources under
[`deploy/`](deploy/). Agentplane-managed Haku runners use their own template and bootstrap.

## Former deployment configuration

The old `agent_sandbox` Console config selected the `haku` pool, `workspace` container,
`/workspace/haku-state` working directory, an eight-hour claim TTL with two-hour exec renewals,
and the image's bootstrap script. Its schema remains in [`config.py`](config.py) as historical
reference; it is no longer accepted by the active Console config.

The retired deployment package records the final Haku-specific resources from the generated chart.
It has no Flux registration. Read [`deploy/README.md`](deploy/README.md) before using those files.

## Reactivation boundary

Treat the code, image, and manifests as historical reference. A future use would need a new design
for sandbox identity and Kubernetes grants, an explicit decision to restore a Haku-specific
sandboxing path, and fresh review of the old role and credentials. Do not revive the warm pool as
part of the Agentplane migration.
