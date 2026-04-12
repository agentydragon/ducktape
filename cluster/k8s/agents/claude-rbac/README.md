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

- harbor, langfuse, ollama (read + consumer), openclaw, props, gatus
- Logs/configmaps in monitoring, kube-system, longhorn-system, flux-system, grocy-mcp

## ServiceAccount

- **Name**: `claude-code-web`
- **Namespace**: `default`

## Generating a Kubeconfig

To generate a kubeconfig for Claude to use:

```bash
# Create a token (valid for 1 year)
kubectl create token claude-code-web -n default --duration=8760h > /tmp/claude-token.txt

# Get cluster info
CLUSTER_NAME=$(kubectl config view --minify -o jsonpath='{.clusters[0].name}')
SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')

# Get CA certificate
kubectl config view --raw --minify --flatten \
  -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' \
  | base64 -d > /tmp/ca.crt

# Generate kubeconfig
cat <<EOF > /tmp/claude-kubeconfig.yaml
apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority-data: $(base64 -w0 < /tmp/ca.crt)
    server: $SERVER
  name: $CLUSTER_NAME
contexts:
- context:
    cluster: $CLUSTER_NAME
    user: claude-code-web
    namespace: default
  name: claude-code-web
current-context: claude-code-web
users:
- name: claude-code-web
  user:
    token: $(cat /tmp/claude-token.txt)
EOF

chmod 600 /tmp/claude-kubeconfig.yaml
```

## Kubeconfig Provisioning

Kubeconfig is generated automatically by the session start hook via
`devinfra/claude/hook_daemon/session_start/secret_sources.py`. The SA token is stored as a k8s Secret in the
`claude-sandbox` namespace and read at session start. No manual encryption needed.

## Security Considerations

- **Write isolation**: Full CRUD only in `claude-sandbox` namespace
- **Broad read**: Cluster-wide diagnostics read (nodes, pods, Flux, certs, metrics, etc.)
- **Resource quotas**: 8 CPU, 16Gi memory, 20 pods (see <resourcequota.yaml>)
- **Flux patch**: Can trigger Flux reconciliation via annotation patch (Kyverno policy
  restricts to annotation-only patches)
