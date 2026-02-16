# Claude Cluster-Wide Read-Only RBAC

This directory configures cluster-wide read-only RBAC for the Claude AI assistant.

**Depends on**: `claude-rbac` (which creates the `claude-readonly` ServiceAccount)

## Permissions Granted

**Cluster-wide (read-only):**

- ✅ Read pod logs (`kubectl logs`)
- ✅ Read cluster state (pods, deployments, services, configmaps, etc.)
- ✅ Read Flux resources (kustomizations, gitrepositories)
- ❌ **NO direct secrets access**
- ❌ **NO write permissions** (create, update, delete)
- ❌ **NO exec access** cluster-wide

## Deployment

This configuration should be deployed **after** `claude-rbac` (the sandbox namespace setup):

```bash
# Apply sandbox first (creates ServiceAccount)
kubectl apply -k cluster/k8s/claude-rbac

# Then apply cluster-wide read-only
kubectl apply -k cluster/k8s/claude-rbac-read-only
```

Or add to root kustomization:

```yaml
resources:
  - claude-rbac/flux-kustomization.yaml
  - claude-rbac-read-only/flux-kustomization.yaml # Optional
```

## Security Considerations

**Direct secret access vs indirect exposure:**

The RBAC restrictions prevent direct `kubectl get secrets` cluster-wide, but Claude can still access secrets through side channels where permitted:

- **Pod environment variables**: `kubectl describe pod` shows env vars, which may include secrets
- **Pod exec**: `kubectl exec` allows reading mounted secret volumes (if exec is granted)
- **Pod logs**: Applications may inadvertently log secrets
- **ConfigMaps**: Readable cluster-wide (some users mistakenly store secrets here)

This is standard K8s security behavior - RBAC on secrets is one layer, but access to pods/logs provides indirect paths. The key security boundary is:

- **Production namespaces**: Read-only access means Claude can observe but not modify
- **claude-sandbox**: Full control for experimentation (if deployed), isolated by namespace + resource quotas

## Testing

```bash
# Should work (read-only cluster-wide)
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml get pods -A
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml logs -n ollama deployment/ollama
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml get kustomizations -n flux-system

# Should fail (no direct secrets access)
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml get secrets -A
# Error: secrets is forbidden

# Should fail (no write permissions)
kubectl --kubeconfig=/tmp/claude-kubeconfig.yaml delete pod some-pod
# Error: pods is forbidden: delete
```
