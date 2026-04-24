# Claude Sandbox Namespace

This directory configures a sandbox namespace for Claude AI assistant with full access
for experimentation.

**Cross-references**: RBAC is referenced from the root `AGENTS.md` (Kubernetes MCP Server
section). Keep both in sync when changing permissions.

## Permissions Granted

### 1. claude-sandbox namespace — full CRUD

Defined in <role-sandbox.yaml>, bound via <rolebinding-sandbox.yaml>:

- Pods: create/delete, logs, exec, attach
- Workloads: deployments, statefulsets, daemonsets, replicasets, jobs, cronjobs
- Config: configmaps, secrets, PVCs, events, services
- ⚠️ **Resource limits** (<resourcequota.yaml>): 8 CPU, 16Gi memory, 20 pods

### 2. Cluster-wide read — diagnostics

`cluster-diagnostics-reader` ClusterRole (<clusterrole-cluster-diagnostics-reader.yaml>),
bound via `shared-rbac/clusterrolebinding-cluster-diagnostics-reader.yaml`:

- Core: nodes, pods, services, endpoints, PVs, PVCs, events, namespaces, resourcequotas
- Workloads: deployments, replicasets, statefulsets, daemonsets, jobs, cronjobs, HPAs, VPAs
- Networking: ingresses, networkpolicies, Gateway API routes, Cilium policies
- Storage: storageclasses, volumeattachments, Longhorn volumes/replicas/nodes
- GitOps: Flux kustomizations (+ patch for reconcile), HelmReleases, git/helm/OCI repos,
  image policies, Terraform resources
- Certs & secrets: cert-manager certificates/issuers, trust-manager bundles, ExternalSecrets
- Monitoring: Prometheus, Alertmanager, ServiceMonitors, metrics API (pods + nodes)
- Other: RBAC roles/bindings, CRDs, webhooks, leases, priority classes, Kyverno policies,
  PowerDNS zones, Vault

### 3. Cross-namespace read

Namespaced Roles + RoleBindings for specific namespaces:

- Namespace diagnostics (pods, logs, services, configmaps, PVCs, events, deployments, replicasets, statefulsets) in harbor, gatus, authentik-mcp-poc, csi-proxmox, openebs, proxmox-proxy, cnpg-system, nvidia-device-plugin, node-feature-discovery, local-path-storage, cert-manager, litellm, docker-ci, matrix, grocy-sf, grocy-vallejo
- Extended read in langfuse, ollama (read + consumer), openclaw, props (+ jobs, constrained secrets)
- Logs/configmaps in monitoring, kube-system, longhorn-system, flux-system, grocy-sf, grocy-vallejo, airlock, authentik

## Authentication

Claude Code web sessions authenticate via an **Authentik-issued OIDC JWT**
that carries `groups: ["kubectl-sandbox-users"]`; kube-apiserver's
`AuthenticationConfiguration` maps that claim to
`oidc-ksbx-groups:kubectl-sandbox-users`, the Group every binding below
subjects on. JWTs are minted biweekly by the `claude-jwt-rotation`
CronJob in the `agents-infra` namespace — see <../claude-jwt-rotation/>.

OIDC users who log into the `kubectl-sandbox-mcp` Authentik application
(interactive MCP) receive the same group claim via the same
`kubectl_sandbox_fixed_groups` scope mapping, so the RBAC below applies
unchanged.

Laptops run their own admin kubeconfig (deployed by home-manager from
`secrets/shared/kubeconfig.yaml`) with cluster-admin-level access; it's
not governed by this sandbox RBAC. See
<../../../docs/lessons_learned/2026_04_24_k8s_auth_through_mitm_proxy.md>
for why Claude Code web can't use the laptop's path (L7 TLS-terminating
egress proxy eats client certs).

## Kubeconfig Provisioning

Kubeconfig is generated automatically by the session start hook via
<devinfra/claude/scripts/write_kubeconfig.py>. It decrypts the SOPS-encrypted
bearer JWT from `secrets/claude-web-k8s-token.yaml` and writes a kubeconfig
with `user.token` auth pointing at `https://kubeapi.allegedly.works` (the
`HTTPRoute` in <../../kube-api-proxy/httproute.yaml>). The JWT is minted
biweekly by the `claude-jwt-rotation` CronJob via Authentik's
`kubectl-sandbox-client-credentials` OAuth2 provider.

## Security Considerations

- **Write isolation**: Full CRUD only in `claude-sandbox` namespace
- **Broad read**: Cluster-wide diagnostics read (nodes, pods, Flux, certs, metrics, etc.)
- **Resource quotas**: 8 CPU, 16Gi memory, 20 pods (see <resourcequota.yaml>)
- **Flux patch**: Can trigger Flux reconciliation via annotation patch (Kyverno policy
  restricts to annotation-only patches)
