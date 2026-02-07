# Observability Umbrella Chart

Installs the namespace (`base`), TimescaleDB, and Grafana components together.

```bash
cd k8s/helm/observability
helm dependency build
helm upgrade --install observability . --namespace observability --create-namespace
```

Override subchart values under the corresponding keys, for example:

```yaml
timescaledb:
  persistence:
    size: 20Gi
```
