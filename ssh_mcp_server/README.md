# SSH MCP server

This standalone SSH MCP server is consumed by Agentplane's Action Service. It exposes two code-owned MCP tools:

- `list_targets` — list every configured `(host, user)` tuple with one aggregate availability flag:
  a target is available only when its configured host-key file exists and its mounted identity is readable;
- `exec` — run one non-interactive command over SSH with an optional timeout bounded by the
  server's configured maximum.

The server owns transport and key selection only. It does not implement a command allowlist or
approval policy; each consumer authorizes the complete call before invoking this backend.

A deferred design for SSH processes that survive a disconnect is in the
[durable processes plan](plans/durable_processes.md).

## Configuration

`SSH_MCP_CONFIG_FILE` points to YAML shaped like this. The example uses placeholder paths; the
cluster deployment's target roster and mounted identity paths are generated from
[`cluster/cdk8s/ssh_mcp/config.py`](../cluster/cdk8s/ssh_mcp/config.py).

```yaml
known_hosts_file: /etc/ssh-mcp/known_hosts
command_timeout_seconds: 300
connect_timeout_seconds: 10
output_limit_bytes: 65536
max_concurrent_executions: 4
targets:
  - host: host.example
    user: operator
    identity_file: /etc/ssh-mcp/keys/host-operator
  - host: other-host.example
    user: operator
    identity_file: /etc/ssh-mcp/keys/other-host-operator
```

Private keys are mounted files, not configuration values. Missing or unreadable identity files
leave their targets in `list_targets` with `available: false`; a missing host-key file does the
same for every target. These conditions do not remove targets or take down the server. Duplicate
`(host, user)` tuples are rejected as invalid configuration.

The HTTP MCP endpoint requires `Authorization: Bearer <SSH_MCP_BEARER_TOKEN>`. The standalone
[deployment](../cluster/k8s/ssh-mcp/README.md) uses one ESO-generated bearer
shared only with the staging Action Service. The endpoint is cluster-internal and has no public route.

Paramiko provides the SSH transport with strict reviewed `known_hosts`, disabled agent/key
search, disabled PTY, and bounded connect/command/output behavior. The remote command is passed
as a single SSH exec request; the server does not run a local shell.
