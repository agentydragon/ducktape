# Valkey Kimsufi Local-Storage Migration

Status: paved through Phase 5 for all operator-managed MCP Valkeys; Manifold,
Grocy SF MCP, Grocy Vallejo MCP, and Tana MCP facade are now on
`local-path-ovh`.
Last live inventory refresh: 2026-05-20.

This note covers two related tasks:

- deciding what blocks decommissioning `talos-vps-worker-0` and
  `talos-vps-worker-1`, plus what still lives on other Hetzner/VPS-backed
  storage;
- moving operator-managed Valkey state to Kimsufi local storage through Flux.

## Current Hetzner/VPS PVCs

These PVCs are currently still backed by Hetzner/VPS storage. PV names are
included because local-path PVs are bound to a specific node and host path.

No operator-managed MCP Valkey PVCs remain on Hetzner storage.

### `talos-vps-worker-0`

| PV                                         | PVC                                        | Pod                     | Purpose                                                                      | Notes                                                                                                                                           |
| ------------------------------------------ | ------------------------------------------ | ----------------------- | ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `pvc-b12db62a-efee-41c9-b158-70a30f766cdd` | `loki/storage-loki-0`                      | `loki-0`                | Loki single-binary local WAL/cache; chunks and indexes use SeaweedFS S3      | Helm values pin singleBinary persistence to `local-path-hetzner` and region `hil`. Needs a Loki-specific migration or accepted cache/WAL loss.  |
| `pvc-87eb5e5d-f588-4b20-870d-946c6da4022b` | `monitoring/storage-mimir-compactor-0`     | `mimir-compactor-0`     | Mimir compactor local working state/cache; long-term blocks use SeaweedFS S3 | StatefulSet with one replica and `local-path-hetzner`. Needs monitoring Helm value migration.                                                   |
| `pvc-1e35cc4f-913d-4d75-99e3-4af241eba2b1` | `monitoring/storage-mimir-ingester-0`      | `mimir-ingester-0`      | Mimir ingester local TSDB/WAL                                                | StatefulSet with one replica and `local-path-hetzner`; this is the riskiest monitoring PVC because it can contain recent samples before upload. |
| `pvc-e8d15fd7-de1d-4887-94ab-4123c77a2785` | `monitoring/storage-mimir-store-gateway-0` | `mimir-store-gateway-0` | Mimir store-gateway cache/local state; blocks use SeaweedFS S3               | StatefulSet with one replica and `local-path-hetzner`. Needs monitoring Helm value migration.                                                   |

### `talos-vps-worker-1`

| PV                                         | PVC                          | Pod                     | Purpose                                                                        | Notes                                                                                               |
| ------------------------------------------ | ---------------------------- | ----------------------- | ------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------- |
| `pvc-c31a81bb-5802-4aff-a267-8befa8f27f81` | `grocy-sf/grocy-config`      | `grocy-77fcb8bdd-grzmj` | Grocy SF application config/data, including SQLite and uploads under `/config` | Deployment uses `Recreate` and region `hil`; migration needs copy/restore or accepted app downtime. |
| `pvc-d1bb043e-a3f1-4d18-999e-0bb2da5a804f` | `grocy-vallejo/grocy-config` | `grocy-77fcb8bdd-tmq9z` | Grocy Vallejo application config/data under `/config`                          | Same pattern as Grocy SF.                                                                           |

### Hetzner Cloud Volumes

| PV                                         | PVC                                      | State      | Pod                         | Purpose                                                     | Notes                                                                                                     |
| ------------------------------------------ | ---------------------------------------- | ---------- | --------------------------- | ----------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `pvc-6bd96776-3bc7-482c-bfae-487b13754982` | `tana-mcp/tana-mcp-config`               | `Bound`    | `tana-mcp-6778d8d56f-nm4xv` | Tana Desktop profile/config under `/home/tana/.config/tana` | `hcloud-volumes` CSI volume with `Retain` reclaim policy and volume handle `105665337`.                   |
| `pvc-abcebdca-d64a-41f6-8cdc-6d5a91ee9922` | `tana-mcp/tana-mcp-facade-fastmcp-state` | `Released` | none                        | Historical Tana MCP facade FastMCP state                    | Released `hcloud-volumes` PV with handle `105665343`; no PVC is currently bound, but the PV still exists. |

### VPS Control-Plane Local PVCs

These are not on the two worker nodes, but they are still local-path PVs pinned
to `talos-vps-cp-*` hosts.

| PV                                         | PVC                                       | Node             | Pod                         | Purpose                                 | Notes                                            |
| ------------------------------------------ | ----------------------------------------- | ---------------- | --------------------------- | --------------------------------------- | ------------------------------------------------ |
| `pvc-036942d3-6286-4325-a99e-580b97916f30` | `authentik/authentik-db-1`                | `talos-vps-cp-0` | `authentik-db-1`            | Authentik Postgres data                 | Generic `local-path`, physically on VPS cp node. |
| `pvc-69f430ae-9d38-4345-9d9d-52d624b5cf7c` | `authentik/authentik-db-2`                | `talos-vps-cp-1` | `authentik-db-2`            | Authentik Postgres data                 | Generic `local-path`, physically on VPS cp node. |
| `pvc-2f501a4b-a898-4151-aa3e-8fb628ac5e9b` | `airlock/airlock-db-2`                    | `talos-vps-cp-0` | `airlock-db-2`              | Airlock Postgres data                   | Generic `local-path`, physically on VPS cp node. |
| `pvc-ac53c38b-d199-466c-aaa5-8ca37172af7a` | `airlock/airlock-db-1`                    | `talos-vps-cp-1` | `airlock-db-1`              | Airlock Postgres data                   | Generic `local-path`, physically on VPS cp node. |
| `pvc-e6154595-d2c4-41f0-b762-65e060449955` | `tofu-state/tofu-state-db-1`              | `talos-vps-cp-0` | `tofu-state-db-1`           | OpenTofu state Postgres data            | Generic `local-path`, physically on VPS cp node. |
| `pvc-184beb25-d123-493d-a4b8-e904e8f2af1d` | `tofu-state/tofu-state-db-2`              | `talos-vps-cp-1` | `tofu-state-db-2`           | OpenTofu state Postgres data            | Generic `local-path`, physically on VPS cp node. |
| `pvc-40b5c07b-4c73-438c-ae88-42b21329489d` | `monitoring/db-alertmanager-monitoring-0` | `talos-vps-cp-0` | `alertmanager-monitoring-0` | Alertmanager notification/silence state | `local-path-hetzner`.                            |
| `pvc-9a6cd0c0-1b45-4cca-a8b3-ec394a9dd147` | `monitoring/db-alertmanager-monitoring-1` | `talos-vps-cp-1` | `alertmanager-monitoring-1` | Alertmanager notification/silence state | `local-path-hetzner`.                            |
| `pvc-b2d9121e-f974-423b-a36b-bff609747771` | `monitoring/grafana-db-1`                 | `talos-vps-cp-1` | `grafana-db-1`              | Grafana Postgres data                   | `local-path-hetzner`.                            |
| `pvc-01eb3b6d-7235-4fd4-9da7-bd3c15f162c6` | `monitoring/grafana-db-2`                 | `talos-vps-cp-0` | `grafana-db-2`              | Grafana Postgres data                   | `local-path-hetzner`.                            |
| `pvc-4fcd7c93-bb5e-47fd-87fe-4c1f3d644c45` | `gatus/gatus`                             | `talos-vps-cp-1` | `gatus-*`                   | Gatus state                             | Generic `local-path`, mounted by active pods.    |

## Operator Facts

Current cluster state:

- HelmRelease: `cluster/k8s/valkey/helmrelease.yaml`
- chart version: `0.24.0`
- running operator image: `quay.io/opstree/redis-operator:v0.25.0`,
  deployed through chart `0.24.0` with image-tag overrides
- local source checkout: `/home/agentydragon/code/redis-operator`
- latest local source tag checked: `v0.25.0`
- `quay.io/opstree/redis-operator:v0.25.0` exists; the OT Helm repository
  does not currently publish chart `0.25.0`

Relevant source findings:

- `RedisReplication` has no declarative `spec.masterNode`; the controller
  derives the master from live Valkey roles and writes `.status.masterNode`.
- The `*-master` service selects pods by the `redis-role=master` label; labels
  are reconciled by connecting to the pods and reading their live role.
- StatefulSet ordinals are still `0..N-1`; regular scale-down removes the
  highest ordinal first, so YAML cannot say "remove ordinal 0 but keep ordinal
  1" for the existing Valkey StatefulSet.
- `v0.25.0` is useful but does not solve declarative promotion. It improves
  master fallback when all replication pods restart and fixes Sentinel config
  persistence across container restarts.
- `additionalRedisConfig` is only reliable for our `valkey/valkey:8-alpine`
  pods when `GenerateConfigInInitContainer=true`, because the init container
  generates `/etc/redis/redis.conf` and starts Valkey with that file.

TODO: replace the chart `0.24.0` plus image-tag override with a normal chart
version bump after the OT Helm repository publishes a `redis-operator` chart
for `v0.25.0` or newer.

If we bump for this migration, use it as a Phase 0 hardening step. Keep the
chart at `0.24.0` and override the operator/init image tags:

```yaml
values:
  redisOperator:
    imageTag: v0.25.0
    initContainerImageTag: v0.25.0
  featureGates:
    GenerateConfigInInitContainer: true
```

## Why Not "Just YAML" On The Existing CR

For the current `manifold-valkey` pair, the old master is
`manifold-valkey-0` on `talos-vps-worker-0`; the Kimsufi replica is
`manifold-valkey-1`.

A same-CR Git change can pin future pods to Kimsufi, but it cannot express all
of this atomically:

1. promote `manifold-valkey-1`;
2. make `manifold-valkey-0` follow it;
3. delete only the VPS-bound ordinal/PVC;
4. keep the service endpoint stable throughout.

The operator has no desired-master field, and StatefulSet semantics do not let
us remove ordinal 0 while keeping ordinal 1. The low-downtime way to do that is
a data-plane command (`REPLICAOF NO ONE` on the Kimsufi replica and
`REPLICAOF <new-master>` on the old master), whether issued by `kubectl exec`
or by a Git-applied Job. That keeps the existing service name, but it is still
an imperative role change.

## GitOps Replacement Path

This path avoids imperative promotion by creating a new Kimsufi-only
`RedisReplication`, letting it replicate from the old master, then cutting the
consumer to the new service. It does not mutate the existing StatefulSet
ordinals.

This is suitable for the MCP facade Valkeys because they hold OAuth/session
state. For strictly lossless state, insert a brief app write pause before the
detach/cutover step. Without that pause, writes that land on the old master
after the final catch-up check but before the app restarts against the new
Valkey can be lost.

### Phase 0: Operator Prep

Decide whether to bump the operator image and enable
`GenerateConfigInInitContainer`. If enabling the feature gate, roll this out and
verify the existing Valkey pods stay healthy before creating any replacement
Valkey:

```bash
kubectl -n valkey-system get deploy redis-operator -o wide
kubectl get redisreplication -A -o wide
kubectl get pods -A -l redis_setup_type=replication -o wide
```

### Phase 1: Add Kimsufi Followers

For `manifold-mcp`, add a second Valkey CR with a distinct name and Kimsufi-only
storage. The extra config makes both new pods start as replicas of the old
master service. Use size 2 so the final replacement remains replicated across
the two Kimsufi workers.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: manifold-valkey-kimsufi-replica-config
  namespace: manifold-mcp
data:
  redis-additional.conf: |
    replicaof manifold-valkey-master.manifold-mcp.svc.cluster.local 6379
---
apiVersion: redis.redis.opstreelabs.in/v1beta2
kind: RedisReplication
metadata:
  name: manifold-valkey-kimsufi
  namespace: manifold-mcp
  annotations:
    description: Replacement Kimsufi Valkey for Manifold MCP OAuth state
spec:
  clusterSize: 2
  redisConfig:
    additionalRedisConfig: manifold-valkey-kimsufi-replica-config
  kubernetesConfig:
    image: valkey/valkey:8-alpine
    imagePullPolicy: IfNotPresent
    resources:
      requests:
        cpu: 50m
        memory: 64Mi
      limits:
        cpu: 200m
        memory: 128Mi
  storage:
    volumeClaimTemplate:
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: local-path-ovh
        resources:
          requests:
            storage: 1Gi
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              - key: topology.kubernetes.io/zone
                operator: In
                values: ["hil-ovh"]
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector:
            matchLabels:
              app: manifold-valkey-kimsufi
          topologyKey: kubernetes.io/hostname
```

Commit and push through Flux. Verify the new pod is on a Kimsufi worker and is
replicating from the old service:

```bash
kubectl -n manifold-mcp get pod -l app=manifold-valkey-kimsufi -o wide
kubectl -n manifold-mcp exec manifold-valkey-kimsufi-0 -- valkey-cli role
kubectl -n manifold-mcp exec manifold-valkey-kimsufi-1 -- valkey-cli role
kubectl -n manifold-mcp exec manifold-valkey-kimsufi-0 -- valkey-cli info replication
kubectl -n manifold-mcp exec manifold-valkey-kimsufi-0 -- valkey-cli info persistence
```

Expected: role is `slave`, `master_link_status:up`, and persistence is enabled.

### Phase 2: Stop Writers

Pause the consumer by Git before detaching the new Valkey. This avoids losing
writes between the final replication catch-up check and the app restart against
the new service:

```yaml
spec:
  replicas: 0
```

Commit, push, and wait until the app pod is gone. Then re-check the replacement
Valkey's replication status.

### Phase 3: Detach The Replacement

In one Git change:

- remove `redisConfig.additionalRedisConfig` from
  `manifold-valkey-kimsufi`;
- remove the replica config `ConfigMap` from kustomization;

Push and wait for Flux. The two replacement pods will restart as standalone
masters, then the operator should choose one as master and configure the other
as a replica. Verify before unpausing the app:

```bash
kubectl -n manifold-mcp get pod -l app=manifold-valkey-kimsufi -L redis-role -o wide
kubectl -n manifold-mcp get endpointslice -l kubernetes.io/service-name=manifold-valkey-kimsufi-master -o wide
kubectl -n manifold-mcp exec manifold-valkey-kimsufi-0 -- valkey-cli role
kubectl -n manifold-mcp exec manifold-valkey-kimsufi-1 -- valkey-cli role
```

Expected: exactly one pod is `redis-role=master`, the new master service has
one endpoint, and the other pod is a replica.

### Phase 4: Cut Over And Unpause

In one Git change:

- set `MCP_FACADE_PERSISTENCE__HOST` to
  `manifold-valkey-kimsufi-master.manifold-mcp.svc.cluster.local`;
- restore the consumer deployment to `replicas: 1`.

Push and wait for Flux. Verify:

```bash
kubectl -n manifold-mcp get deploy manifold-mcp
kubectl -n manifold-mcp logs deploy/manifold-mcp -c facade --tail=100
```

Expected: the facade is healthy and using the new Kimsufi Valkey service.

## First Run Notes

The Manifold MCP trial was run on 2026-05-19 with these Git commits:

- Phase 0 operator prep: `d1b33344f`
- Phase 1 Kimsufi followers: `1e2a0531b`
- Phase 2 app pause: `6b6cebd11`
- Phase 3 replacement detach: `6b78c31fd`
- Phase 4 app cutover and unpause: `8ed2af13c`
- Phase 5 old Valkey retirement: `a707f34dd`

Observed results:

- Phase 0 rolled the existing Valkeys and they recovered.
- Phase 1 created two `local-path-ovh` PVCs on
  `talos-kimsufi-worker-0` and `talos-kimsufi-worker-1`.
- Phase 3 did roll the replacement pods after removing
  `additionalRedisConfig`; no extra rollout nudge was needed.
- Phase 4 restored `manifold-mcp` to `1/1`; the running facade pod had
  `MCP_FACADE_PERSISTENCE__HOST=manifold-valkey-kimsufi-master.manifold-mcp.svc.cluster.local`
  and public `/healthz` returned `200`.
- Phase 5 removed the old `manifold-valkey` CR, StatefulSet, pods, PVCs, and
  PVs. The only remaining Manifold Valkey storage is the two
  `local-path-ovh` PVCs for `manifold-valkey-kimsufi`.

## Batch Run Notes

The remaining MCP Valkeys were migrated on 2026-05-20 using the same pattern:

- Phase 1 OVH followers: `9f1697338`
- Phase 2 app pause: `cde27fdaa`
- Phase 3 replacement detach: `52c06710a`
- Phase 4 app cutover and unpause: `701cc0997`
- Phase 5 old Valkey retirement: `0ec70be48`

Migrated CRs:

- `grocy-sf/grocy-sf-valkey` -> `grocy-sf/grocy-sf-valkey-ovh`
- `grocy-vallejo/grocy-vallejo-valkey` ->
  `grocy-vallejo/grocy-vallejo-valkey-ovh`
- `tana-mcp/mcp-valkey` -> `tana-mcp/mcp-valkey-ovh`

Observed results:

- All replacement pods landed on `talos-kimsufi-worker-0` and
  `talos-kimsufi-worker-1` with `local-path-ovh` PVCs.
- Removing `additionalRedisConfig` rolled the replacement StatefulSets and the
  operator selected ordinal `0` as master, with ordinal `1` as replica.
- The Grocy MCP services do not expose `/healthz`; `/mcp` returned the expected
  unauthenticated `401`, and pod logs showed clean startup. Tana MCP facade
  `/healthz` returned `200`.
- Flux sometimes reported a stale `gateway` dependency gate; reconciling
  `gateway` first cleared it, after which the MCP Kustomizations applied
  normally.

### Phase 5: Retire Old Valkey

After the consumer has been stable on the new service, remove the old
`manifold-valkey` CR from Git. The local-path PVs use `Delete` reclaim policy,
so the old VPS-bound PV should disappear after the CR and PVC are removed.

Verify no remaining Manifold Valkey storage is on a VPS worker:

```bash
kubectl get pv -o wide
kubectl -n manifold-mcp get pvc
kubectl -n manifold-mcp get pod -o wide
```

## Rollback

Before Phase 3, delete the replacement CR and ConfigMap from Git; the consumer
still points at the old `manifold-valkey-master` service.

After Phase 4, rollback is a Git change that points
`MCP_FACADE_PERSISTENCE__HOST` back to
`manifold-valkey-master.manifold-mcp.svc.cluster.local`. Any writes accepted by
the replacement after cutover will not automatically flow back to the old
Valkey.

## Paving Protocol For The First Run

Use `manifold-mcp` as the trial because its old master is the only Valkey master
currently on `talos-vps-worker-0`, and the state is OAuth/session persistence.

Before pushing live changes:

1. verify whether the Helm repository exposes an operator chart newer than
   `0.24.0`, and whether `quay.io/opstree/redis-operator:v0.25.0` exists;
2. pause `manifold-mcp` during detach and cutover;
3. commit Phase 0 by itself if enabling the feature gate or bumping the image;
4. run Phases 1 through 5 as separate Git commits, recording every Flux or
   operator surprise back into this runbook.
