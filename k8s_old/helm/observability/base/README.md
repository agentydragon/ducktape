# observability-base Helm Chart

Creates the `observability` namespace (or a custom name via values).

```bash
cd k8s/helm/observability/base
helm dependency build
helm upgrade --install observability-base .
```
