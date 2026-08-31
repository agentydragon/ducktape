# TODO: Allow MCP k8s servers to read/list secrets

The in-cluster `kubectl-passthrough-mcp` cannot list/read secrets. This forces falling back to
`kubectl` via Bash for secret operations
(deletion, inspection, rotation).

**Symptom**: `secrets is forbidden: User "..." cannot list resource "secrets" in API group ""`

**Likely fix**: RBAC for the MCP server service accounts / client certs needs `secrets` added
to the allowed resources. Check:

1. `cluster/k8s/agents/agent-rbac-base/role-sandbox.yaml` — add `secrets` to the `claude-sandbox` role
2. The MCP client identity and its Kubernetes RBAC may need to grant secret access
   **Why it matters**: Every suspension/deletion workflow needs secret access. Falling back to
   `kubectl` via Bash works but defeats the purpose of having dedicated MCP servers.
