# Claude Sandbox Namespace

This directory configures a sandbox namespace for Claude AI assistant with full access for experimentation.

## Permissions Granted

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
# Should work (full access in sandbox)
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml -n claude-sandbox create deployment nginx --image=nginx
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml -n claude-sandbox get pods
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml -n claude-sandbox create secret generic test --from-literal=key=value
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml -n claude-sandbox get secrets
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml -n claude-sandbox exec -it <pod> -- /bin/bash

# Should fail (no permissions outside sandbox without cluster-wide RBAC)
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml get pods -A
# Error: pods is forbidden
```

## Security Considerations

The sandbox provides an isolated environment with resource limits:

- **Namespace isolation**: Only `claude-sandbox` namespace is accessible
- **Resource quotas**: 4 CPU, 8Gi memory, 10 pods max
- **Full control**: Create/delete/modify any resources including secrets within sandbox
- **No cluster access**: Without additional cluster-wide RBAC, access is limited to sandbox only

To grant cluster-wide read access, deploy the separate `claude-rbac-read-only` configuration.
