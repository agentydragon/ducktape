# Claude Sandbox RBAC

This directory is the **lightweight base** for Claude agent sandbox RBAC. It contains only
cluster-scoped resources (ClusterRoles) and sandbox-internal resources (namespace + RoleBindings
within `claude-sandbox`). It must **never** depend on service or database kustomizations.

Namespace-scoped RoleBindings targeting other namespaces live in per-service `agent-rbac/`
directories (see Architecture below).

**Cross-references**: this README is `@`-transcluded into `cluster/AGENTS.md`; the root
`AGENTS.md` (Kubernetes MCP Server section) keeps a brief MCP-usage summary that points
here. Update this file as the single source when changing permissions or the quota.

## Architecture

Agent RBAC is split across three layers:

### 1. `claude-rbac` (this directory) — lightweight base

Depends on: `kyverno-policies` only. No service or database dependencies.

Contains:

- `claude-sandbox` namespace, ResourceQuota, LimitRange
- Sandbox-internal Role + RoleBinding (`role-sandbox.yaml`, `rolebinding-sandbox.yaml`)
- Sandbox-internal `rolebinding-ollama-consumer.yaml` (binds ClusterRole in claude-sandbox ns)
- Three ClusterRoles: `cluster-diagnostics-reader`, `logs-configmaps-reader`,
  `namespace-diagnostics-reader`

### 2. `shared-rbac` — cluster-scoped bindings

Depends on: `claude-rbac`, `kyverno-policies`.

Contains:

- `ClusterRoleBinding` for `cluster-diagnostics-reader` (cluster-wide)
- `RoleBinding` for `logs-configmaps-reader` in `flux-system` only

### 3. Per-service `<service>/agent-rbac/` — namespace-scoped RoleBindings

Each service that grants agent read access has its own `agent-rbac/` directory with an
independent Flux kustomization. Depends on: `[service's namespace kustomization]` + `claude-rbac`.

This isolation ensures that missing/suspended service namespaces don't block unrelated RBAC
from applying.

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
- Storage: storageclasses, volumeattachments
- GitOps: Flux kustomizations (+ patch for reconcile), HelmReleases, git/helm/OCI repos,
  image policies, Terraform resources
- Certs & secrets: cert-manager certificates/issuers, trust-manager bundles, ExternalSecrets
- Monitoring: Prometheus, Alertmanager, ServiceMonitors, metrics API (pods + nodes)
- Other: RBAC roles/bindings, CRDs, webhooks, leases, priority classes, Kyverno policies,
  PowerDNS zones

### 3. Cross-namespace read

Namespaced RoleBindings live in per-service `agent-rbac/` directories. Each is an independent
Flux kustomization that depends only on the target namespace + `claude-rbac`.

@permissions.md

## Adding Agent RBAC for a New Service

1. Create `<service>/agent-rbac/` with:
   - `flux-kustomization.yaml` — depends on service's namespace kustomization + `claude-rbac`
   - `kustomization.yaml` — lists the RoleBinding YAML(s)
   - RoleBinding YAML(s) referencing the appropriate ClusterRole from `claude-rbac/`
2. Add the `flux-kustomization.yaml` path to the root `cluster/k8s/kustomization.yaml`
3. The service namespace kustomization has **zero coupling** to agent infrastructure

## Authentication

Claude Code web sessions authenticate via an **Authentik-issued OIDC JWT**
that carries `groups: ["kubectl-sandbox-users"]`; kube-apiserver's
`AuthenticationConfiguration` maps that claim to
`oidc-ksbx-groups:kubectl-sandbox-users`, the Group every binding below
subjects on. JWTs are minted biweekly by the `authentik-jwt-rotation`
CronJob in the `agents-infra` namespace — see <../authentik-jwt-rotation/>.

OIDC users who log into the `kubectl-sandbox-mcp` Authentik application
(interactive MCP) receive the same group claim via the same
`kubectl_sandbox_fixed_groups` scope mapping, so the RBAC below applies
unchanged.

Machine JWTs from the `kubectl-sandbox-client-credentials` provider use a
separate explicit Authentik allowlist for effective groups. The normal provider
client-credentials principal maps to `kubectl-sandbox-users`; the `haku-k8s`
service account maps to `haku`; unknown machine principals receive no Kubernetes
RBAC group.

Laptops run their own admin kubeconfig (deployed by home-manager from
`secrets/shared/kubeconfig.yaml`) with cluster-admin-level access; it's
not governed by this sandbox RBAC. See
<../../../docs/lessons_learned/2026_04_24_k8s_auth_through_mitm_proxy.md>
for why Claude Code web can't use the laptop's path (L7 TLS-terminating
egress proxy eats client certs).

## Kubeconfig Provisioning

The Claude web JWT is minted by the `authentik-jwt-rotation` CronJob via
Authentik's `kubectl-sandbox-client-credentials` OAuth2 provider and committed
SOPS-encrypted to `secrets/claude-web-k8s-jwt.yaml`. At session start,
<devinfra/k8s/kubeconfig.py> decrypts it and builds a kubeconfig
with `user.token` auth pointing at `https://kubeapi.allegedly.works`
(the `HTTPRoute` in <../../kube-api-proxy/httproute.yaml>).

Two callers, different kubeconfig materialization strategies:

- **Session start hook** (background command in the web profile): writes
  `~/.kube/config` to a real file for interactive `kubectl` use.
- **`kubectl-local` MCP server** (<devinfra/claude/kubectl_local_mcp.py>):
  writes the kubeconfig into an anonymous `memfd_create` file with no
  filesystem path. The fd is passed as `--kubeconfig /proc/self/fd/<N>`
  and is inherited across `execvp` into `kubernetes-mcp-server`; it
  disappears when the server exits.

## Security Considerations

- **Write isolation**: Full CRUD only in `claude-sandbox` namespace
- **Broad read**: Cluster-wide diagnostics read (nodes, pods, Flux, certs, metrics, etc.)
- **Resource quotas**: 8 CPU, 16Gi memory, 20 pods (see <resourcequota.yaml>)
- **Flux patch**: Can trigger Flux reconciliation via annotation patch (Kyverno policy
  restricts to annotation-only patches)
