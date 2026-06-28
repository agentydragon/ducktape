# Wyrm2 Flapping Fixes — 2026-05-14

Inventory of cluster issues actively generating noise while wyrm2 is down.

## Stale Terraform State Locks

Runner pods killed when wyrm2 went down left ~10 state locks. Every reconcile hits
`TFExecPlanFailed`.

- [ ] Force-unlock stale locks: `sso-providers`, `agent-machine-access`, `authentik-mcp-poc`,
      `harbor-oidc-config`, `ollama-bearer-token`, `litellm-api-key`, `gatus-sso`
- [ ] Verify downstream kustomizations recover (airlock, headlamp, ollama, gatus, MCP servers)

## Agent RBAC Base Namespace References

OpenClaw namespaces were deleted during wyrm2 suspension but the agent RBAC base still references
them.

- [ ] Update `cluster/k8s/agents/agent-rbac-base/` to remove or condition the `openclaw-gateway`
      RoleBinding
- [ ] Check `agent-shared-rbac` for stale `docker-ci` namespace reference
- [ ] Verify downstream recovers (agent-shared-secrets, kubectl-\*-mcp, tana-mcp-facade,
      manifold-mcp, grocy-mcp-\*)

## hcloud-csi HelmRelease

Upgrade to `hcloud-csi@2.19.1` failed (context deadline exceeded), stuck in
`RetriesExceeded`.

- [ ] `flux reconcile helmrelease hcloud-csi -n kube-system --force`

## Wyrm2 Downstream (won't fix until wyrm2 returns)

These are blocked on Proxmox node availability. No action needed now, noise stops once
wyrm2 is back.

- harbor-db, atuin-db, attic-db — CNPG pods Pending (PVCs on wyrm2 volumes)
- ollama — GPU workload, needs wyrm2
- proxmox-proxy — suspended in git
