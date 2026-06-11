**Namespace diagnostics** (`namespace-diagnostics-reader` ClusterRole bound per-namespace):
harbor, gatus, csi-proxmox, openebs, proxmox-proxy, cnpg-system,
nvidia-device-plugin, node-feature-discovery, local-path-storage, cert-manager, litellm,
docker-ci, matrix, grocy-sf, grocy-vallejo, study-casino, props, tana-mcp,
wayback-cache

**Extended read**: ollama (`rolebinding-ollama-reader.yaml` in `ollama/agent-rbac/`),
langfuse (in `langfuse/agent-rbac/`), openclaw (in `openclaw/gateway-agent-rbac/`),
props (Role + RoleBinding in `props/agent-rbac/`)

**Logs/configmaps** (`logs-configmaps-reader` ClusterRole bound per-namespace):
monitoring, kube-system, grocy-sf, grocy-vallejo, airlock, authentik,
augur (plus `flux-system` in `shared-rbac/`). The augur binding lives cross-repo
in `gaffer-private/k8s/augur/agent-rbac/` since augur itself is reconciled from
gaffer-private. The augur agent-rbac directory also defines an in-namespace Role
granting `pods/exec`, `pods/attach`, and `pods/portforward` for debugging the
single-replica augur deployment.
