# TODO: Allow MCP k8s servers to read/list secrets

Some MCP callers have encountered secret-access denials, forcing a fallback to `kubectl`
with a different identity. Identify the caller and namespace before changing permissions.

**Symptom**: `secrets is forbidden: User "..." cannot list resource "secrets" in API group ""`

The passthrough server forwards the caller's Authentik JWT; its own service account is
unprivileged. The `claude-sandbox` role already includes Secrets, and cross-namespace
grants are explicit opt-ins. See <agent_rbac.md> and
<../cdk8s/kubectl_passthrough_mcp.py>.

Next, reproduce the denial with the affected caller, record the exact identity, verb and
namespace, and determine whether that caller should receive the specific grant.
