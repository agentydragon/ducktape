This file intentionally avoids a hand-maintained namespace matrix. The source of
truth is the set of `*rolebinding-*.yaml` files under:

- `cluster/k8s/**/agent-rbac/`
- `cluster/k8s/**/gateway-agent-rbac/`
- `cluster/k8s/agents/shared-rbac/`

Use `roleRef.name` in those files to determine which permission class is bound:
`namespace-diagnostics-reader`, `logs-configmaps-reader`, `secrets-reader`
(explicit per-service opt-in, kubectl-sandbox-users only — never Haku), or a
service-specific reader Role/ClusterRole such as `ollama-reader`,
`langfuse-log-reader`, or `claude-props-reader`.

Augur is reconciled from `gaffer-private`, so its agent RBAC lives cross-repo at
`gaffer-private/k8s/augur/agent-rbac/`. That directory also defines an
in-namespace Role granting `pods/exec`, `pods/attach`, and `pods/portforward` for
debugging the single-replica augur deployment.
