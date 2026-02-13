# Claude Hooks Secrets

This directory contains age-encrypted secrets that are decrypted during Claude Code session startup.

## Structure

Each `*.age` file decrypts to a JSON object mapping environment variable names to values:

```json
{
  "ENV_VAR_NAME": "value",
  "ANOTHER_VAR": "another value"
}
```

## Kubeconfig Setup

To provide Claude with kubectl access:

### 1. Generate Kubeconfig

```bash
# Create ServiceAccount token (1 year validity)
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
```

### 2. Encrypt and Store

```bash
# Base64 encode the kubeconfig
KUBECONFIG_B64=$(base64 -w0 < /tmp/claude-kubeconfig.yaml)

# Get age public key (from HookSettings or generate one)
AGE_PUBKEY="age1..."  # Your age public key

# Create secret JSON
cat <<EOF > /tmp/kubeconfig-secret.json
{
  "KUBECONFIG_B64": "$KUBECONFIG_B64"
}
EOF

# Encrypt with age
age -r "$AGE_PUBKEY" -o .claude_hooks/secrets/kubeconfig.age < /tmp/kubeconfig-secret.json

# Clean up
rm /tmp/claude-kubeconfig.yaml /tmp/kubeconfig-secret.json /tmp/claude-token.txt /tmp/ca.crt
```

### 3. Integration

The `kubeconfig_setup.py` module automatically:

1. Reads `KUBECONFIG_B64` from decrypted secrets
2. Writes it to `~/.cache/claude-hooks/kubeconfig`
3. Sets `KUBECONFIG` environment variable
4. All kubectl commands in the session use this config

## Security

- Secrets are encrypted with age (X25519)
- Decryption key is provided via `DUCKTAPE_CLAUDE_HOOKS_SECRETS_AGE_KEY` env var
- Kubeconfig provides access to `claude-sandbox` namespace only (by default)
- Resource quotas limit what can be created in the sandbox

## Permissions

The `claude-readonly` ServiceAccount has:

**claude-sandbox namespace (full access):**

- ✅ Create/delete pods, deployments, jobs, secrets
- ✅ kubectl exec into pods
- ✅ Full secrets access (create, read, update, delete)
- ✅ View logs
- Limited by ResourceQuota: 4 CPU, 8Gi memory, 10 pods max

**No cluster-wide access** by default. To grant cluster-wide read-only access, deploy the separate `claude-rbac-read-only` configuration.
