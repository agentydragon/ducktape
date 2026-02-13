# Claude Read-Only RBAC

This directory configures RBAC for the Claude AI assistant to have read-only access to the Kubernetes cluster.

## Permissions Granted

**Cluster-wide (read-only):**

- ✅ Read pod logs (`kubectl logs`)
- ✅ Read cluster state (pods, deployments, services, configmaps, etc.)
- ✅ Read Flux resources (kustomizations, gitrepositories)
- ❌ **NO access to secrets**
- ❌ **NO write permissions** (create, update, delete)
- ❌ **NO exec access** cluster-wide

**claude-sandbox namespace (full access):**

- ✅ Create/delete pods, deployments, services, jobs
- ✅ Full secrets access (create, read, update, delete)
- ✅ `kubectl exec` into pods
- ✅ Attach to pods, view logs
- ⚠️ **Resource limits:** 4 CPU, 8Gi memory, 10 pods max (enforced by ResourceQuota)

## ServiceAccount

- **Name**: `claude-readonly`
- **Namespace**: `default`

## Generating a Kubeconfig

To generate a kubeconfig for Claude to use:

```bash
# Create a token (valid for 1 year)
kubectl create token claude-readonly -n default --duration=8760h > /tmp/claude-token.txt

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
    user: claude-readonly
    namespace: default
  name: claude-readonly
current-context: claude-readonly
users:
- name: claude-readonly
  user:
    token: $(cat /tmp/claude-token.txt)
EOF

chmod 600 /tmp/claude-kubeconfig.yaml
```

## Encrypting the Kubeconfig (Optional)

To store the kubeconfig securely in the repository:

```bash
# Encrypt with age
age -e -r <your-public-key> /tmp/claude-kubeconfig.yaml \
  > tools/claude_hooks/secrets/kubeconfig.age
```

## Testing Permissions

```bash
# Should work
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml get pods -A
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml logs -n ollama deployment/ollama

# Should fail (no direct secrets access cluster-wide)
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml get secrets -A
# Error: secrets is forbidden

# Should fail (no write permissions cluster-wide)
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml delete pod some-pod
# Error: pods is forbidden: delete

# Should work (full access in sandbox)
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml -n claude-sandbox create secret generic test --from-literal=key=value
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml -n claude-sandbox get secrets
```

## Security Considerations

**Direct secret access vs indirect exposure:**

The RBAC restrictions prevent direct `kubectl get secrets` cluster-wide, but Claude can still access secrets through side channels where permitted:

- **Pod environment variables**: `kubectl describe pod` shows env vars, which may include secrets
- **Pod exec**: `kubectl exec` allows reading mounted secret volumes
- **Pod logs**: Applications may inadvertently log secrets
- **ConfigMaps**: Readable cluster-wide (some users mistakenly store secrets here)

This is standard K8s security behavior - RBAC on secrets is one layer, but access to pods/logs provides indirect paths. The key security boundary is:

- **Production namespaces**: Read-only access means Claude can observe but not modify
- **claude-sandbox**: Full control for experimentation, isolated by namespace + resource quotas
