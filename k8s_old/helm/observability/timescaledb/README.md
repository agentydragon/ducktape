# observability-timescaledb Helm Chart

Provision the TimescaleDB instance backing the observability stack.

```bash
cd k8s/helm/observability/timescaledb
helm dependency build
helm upgrade --install observability-timescaledb . --namespace observability
```

`values.yaml` exposes storage sizing, image selection, and the embedded sealed secret. Leave `sealedSecret.enabled=true` to apply the Git-tracked credential or regenerate a new encrypted value with `kubeseal`.

The original Timescale schema is preserved under `files/schema.sql` for manual migrations (`kubectl exec ... -- psql < schema.sql`).
