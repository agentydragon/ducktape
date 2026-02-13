# Claude Read-Only RBAC

This directory configures RBAC for the Claude AI assistant to have read-only access to the Kubernetes cluster.

## Permissions Granted

- ✅ Read pod logs (`kubectl logs`)
- ✅ Read cluster state (pods, deployments, services, configmaps, etc.)
- ✅ Read Flux resources (kustomizations, gitrepositories)
- ❌ **NO access to secrets**
- ❌ **NO write permissions** (create, update, delete)
- ❌ **NO exec access** (`kubectl exec`)

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

# Should fail (no access to secrets)
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml get secrets -A
# Error: secrets is forbidden

# Should fail (no write permissions)
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml delete pod some-pod
# Error: pods is forbidden: delete
```
