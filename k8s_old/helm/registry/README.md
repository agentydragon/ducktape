# Registry Helm Chart

Helm packaging for the in-cluster Docker registry.

## Features

- Registry deployment with configurable replica count, resources, and extra environment variables.
- Persistent storage with optional reuse of an existing PVC.
- Optional external LoadBalancer service (MetalLB compatible) alongside the internal ClusterIP service.
- TLS-enabled ingress with configurable annotations for large uploads.

## Usage

```bash
cd k8s/helm/registry
helm dependency update
helm template registry . --namespace registry
helm upgrade --install registry . --namespace registry --create-namespace
```

Override defaults with your own values file, for example to keep the existing PVC:

```yaml
persistence:
  enabled: true
  existingClaim: registry-storage
```

Disable the external service if you only use ingress:

```yaml
loadBalancerService:
  enabled: false
```

## Migration Notes

- The legacy manifests under `k8s/registry/` are superseded by this chart; refer to that directory only for historical context.
- Current defaults keep the existing PVC name (`registry-storage`) so data is reused automatically.
- The chart expects Secrets/ConfigMaps to be managed externally; supply extra environment variables via `values.yaml` if needed.
