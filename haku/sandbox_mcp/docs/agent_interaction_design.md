# Agent interaction design

## Workflows

The common path is two calls: `provision_sandbox(name)` creates or resumes a
named environment and completes its reviewed bootstrap, then
`exec_sandbox(name, ...)` performs work. Provisioning can return a nonterminal
resource or bootstrap state when Kubernetes takes longer than the configured
wait; the caller polls `get_sandbox_info` or retries provisioning.

`list_sandboxes` discovers existing names with bounded Kubernetes pagination.
`dispose_sandbox` is the explicit cleanup/undo operation.

## Tool contract

- `provision_sandbox` has only the claim name. Environment, lifecycle, and
  bootstrap are deployment policy.
- `exec_sandbox` accepts Bash text, an optional working directory, and explicit
  timeout/output limits. Nonzero exit status is returned as data.
- `get_sandbox_info` and `list_sandboxes` expose compact derived state, never
  raw manifests, environment variables, credentials, or command output.
- `dispose_sandbox` deletes only claims labeled as owned by this server.

All time quantities use seconds. Every exec first confirms a deadline at least
the configured extension into the future; a renewal failure prevents execution.
`state` reports claim/Sandbox/Pod readiness, while `bootstrap_state` reports the
bootstrap lifecycle directly; the two are not collapsed into a synthetic state.

## Retry and failure behavior

Claim names are idempotency keys. A create conflict adopts only a service-owned
claim bearing the current environment contract hash. Configuration drift is
visible through read tools but blocks provisioning and execution until the
claim is disposed and recreated.

Bootstrap scripts are reviewed and run once per claim. Failure leaves the claim
available for inspection, diagnostic exec, or disposal, but provisioning never
reruns the bootstrap. Retrying requires disposing and provisioning a fresh
claim. Expected Kubernetes and lifecycle failures are actionable MCP errors;
unexpected exceptions remain server errors. A bootstrap left marked running
after its configured timeout plus the exec transport grace period is treated
as failed.

## Safety audit

- Mutations never target claims without the server ownership label.
- Lists are paginated and bounded.
- Initial TTL, warm pool, container, and bootstrap cannot be overridden by the
  agent.
- TTL patches use a resource-version precondition and retry conflicts.
- Command output is independently bounded for stdout and stderr.
- Read-only annotations are set only on status/list tools.

## Deferred

Multiple environment profiles, suspend/resume, interactive terminals, file
transfer, port forwarding, and log streaming are intentionally not part of v1.
Deployment manifests and RBAC stay outside this package. Registration behind
haku-console has since landed (`cluster/k8s/haku/console/config.yaml`, the
`sandbox-mcp` entry).
