This file intentionally avoids a hand-maintained namespace matrix. Service-specific and
sensitive grants are sourced from the set of `*rolebinding-*.yaml` files under:

- `cluster/k8s/**/agent-rbac/`
- `cluster/k8s/**/gateway-agent-rbac/`
- `cluster/k8s/agents/shared-rbac/`

Use `roleRef.name` in those files to determine which permission class is bound:
`namespace-diagnostics-reader`, `logs-configmaps-reader`, or `secrets-reader`
(`secrets-reader` is an explicit per-service opt-in for
`kubectl-sandbox-users` only — never Haku), or a
service-specific reader Role/ClusterRole such as `ollama-reader`,
`langfuse-log-reader`, or `claude-props-reader`.

The common `namespace-diagnostics-reader` grant is the exception to the file matrix: a
GitOps-owned Namespace opts in with the
`rbac.ducktape.io/agent-namespace-diagnostics-reader: "true"` label, and
`cluster/k8s/kyverno/policies/generate-agent-diagnostics-readers.yaml` generates one
namespaced RoleBinding for the approved Haku and `kubectl-sandbox-users` identities.
The `kubectl-sandbox-users` group remains a separate Claude Code Web identity.

Augur is reconciled from `gaffer-private`, so its agent RBAC lives cross-repo at
`gaffer-private/k8s/augur/agent-rbac/`. That directory also defines an
in-namespace Role granting `pods/exec`, `pods/attach`, and `pods/portforward` for
debugging the single-replica augur deployment.
