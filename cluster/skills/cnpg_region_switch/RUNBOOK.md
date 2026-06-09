# CNPG Cross-Region Migration via Streaming Replication

Migrate a single-instance CNPG PostgreSQL cluster between regions (or region-pinned storage classes) with sub-second downtime.

Uses CNPG's **Standalone Replica Cluster** pattern: target bootstrapped via `pg_basebackup`, runs as streaming replica, then promoted to independent primary. Irreversible — no demotion back to replica.

## Prerequisites

- CNPG operator installed in cluster
- Source and target storage classes exist and are region-pinned (see <../../docs/cnpg_conventions.md> for the current profiles and region names)
- Both regions' nodes can reach each other over pod network (Nebula mesh)
- `kubectl` access to the namespace
- Same PostgreSQL image version on both clusters (required for physical replication)

## Migration procedure

Set `<SOURCE_STORAGECLASS>` / `<TARGET_STORAGECLASS>` to the region-pinned storage classes for the source and target regions. The procedure is identical in either direction — just swap which region is source vs. target.

### Step 1: Create source cluster (if not existing)

Source cluster runs in the source region with `<SOURCE_STORAGECLASS>` storage.

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: <SOURCE_NAME>
  namespace: <NAMESPACE>
spec:
  instances: 1
  imageName: ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie
  probes:
    liveness:
      isolationCheck:
        enabled: false
  storage:
    storageClass: <SOURCE_STORAGECLASS>
    size: <SIZE>
  monitoring:
    enablePodMonitor: false
  bootstrap:
    initdb:
      database: <DB_NAME>
      owner: <DB_OWNER>
```

Wait for ready:

```bash
kubectl wait cluster/<SOURCE_NAME> -n <NAMESPACE> --for=condition=Ready --timeout=300s
```

### Step 2: Create target cluster as streaming replica

Target cluster in the target region with `<TARGET_STORAGECLASS>` storage, bootstrapped from source via `pg_basebackup`.

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: <TARGET_NAME>
  namespace: <NAMESPACE>
  annotations:
    description: "Streaming replica of <SOURCE_NAME>, to be promoted"
spec:
  instances: 1
  imageName: ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie
  probes:
    liveness:
      isolationCheck:
        enabled: false
  storage:
    storageClass: <TARGET_STORAGECLASS>
    size: <SIZE>
  monitoring:
    enablePodMonitor: false
  replica:
    enabled: true
    source: <SOURCE_NAME>
  bootstrap:
    pg_basebackup:
      source: <SOURCE_NAME>
  externalClusters:
    - name: <SOURCE_NAME>
      connectionParameters:
        host: <SOURCE_NAME>-rw.<NAMESPACE>.svc.cluster.local
        user: streaming_replica
        dbname: postgres
      sslKey:
        name: <SOURCE_NAME>-replication
        key: tls.key
      sslCert:
        name: <SOURCE_NAME>-replication
        key: tls.crt
      sslRootCert:
        name: <SOURCE_NAME>-ca
        key: ca.crt
```

Wait for target to sync:

```bash
kubectl wait cluster/<TARGET_NAME> -n <NAMESPACE> --for=condition=Ready --timeout=600s
```

### Step 3: Verify replication

Check replication lag on target:

```bash
kubectl exec -n <NAMESPACE> <TARGET_POD> -- \
  psql -U postgres -c "SELECT now() - pg_last_xact_replay_timestamp() AS lag;"
```

Lag should be near zero (< 1 second). A null result is normal for idle sources (no recent writes since basebackup).

Verify data present:

```bash
kubectl exec -n <NAMESPACE> <TARGET_POD> -- \
  psql -U postgres -d <DB_NAME> -c "SELECT count(*) FROM <TABLE>;"
```

### Step 4: Promote target

```bash
kubectl patch cluster <TARGET_NAME> -n <NAMESPACE> --type=merge \
  -p '{"spec":{"replica":{"enabled":false}}}'
```

Wait for promotion:

```bash
kubectl wait cluster/<TARGET_NAME> -n <NAMESPACE> --for=condition=Ready --timeout=120s
```

### Step 5: Verify promoted target

```bash
# Confirm primary (not in recovery)
kubectl exec -n <NAMESPACE> <TARGET_POD> -- \
  psql -U postgres -c "SELECT pg_is_in_recovery();"
# Should return: f

# Verify data
kubectl exec -n <NAMESPACE> <TARGET_POD> -- \
  psql -U postgres -d <DB_NAME> -c "SELECT * FROM <TABLE> ORDER BY id;"

# Verify sequences
kubectl exec -n <NAMESPACE> <TARGET_POD> -- \
  psql -U postgres -d <DB_NAME> -c "SELECT last_value FROM <SEQ_NAME>;"

# Verify writable
kubectl exec -n <NAMESPACE> <TARGET_POD> -- \
  psql -U postgres -d <DB_NAME> -c "INSERT INTO <TABLE> (...) VALUES (...);"
```

### Step 6: Update application

Point application at target cluster's service:

```text
<TARGET_NAME>-rw.<NAMESPACE>.svc.cluster.local:5432
```

Update the app's connection string (ConfigMap/Secret/Deployment env) and restart.

### Step 7: Delete source cluster

```bash
kubectl delete cluster <SOURCE_NAME> -n <NAMESPACE>
```

CNPG finalizers clean up PVCs automatically.

## Gotchas

- **Field name**: Use `sslRootCert` (not `sslRootCertificate`) in `externalClusters[]`.
- **Same image version**: Both clusters must use identical PostgreSQL image. Physical replication requires same major version.
- **Peer auth**: Use `psql -U postgres` to connect; app users may fail with peer auth.
- **Services quota**: CNPG creates 3 services per cluster (`-r`, `-ro`, `-rw`). Ensure namespace quota allows ≥6 services during migration (source + target coexist).
- **Standalone replica**: Promotion is irreversible — target cannot demote back to replica.
- **No demotion token needed**: The standalone replica pattern (`spec.replica.enabled: false`) promotes directly. Demotion tokens are only for the Distributed Topology pattern.

## Downtime

Promotion is a single Postgres operation (comparable to HA switchover). Application downtime = time to update connection string + restart.
