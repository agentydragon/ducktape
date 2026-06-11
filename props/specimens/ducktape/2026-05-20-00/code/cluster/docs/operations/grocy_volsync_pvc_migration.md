# Grocy VolSync PVC Migration

Status: smoke-tested with disposable PVCs on 2026-05-20; Grocy SF and Grocy
Vallejo migrated to OVH and verified. Old Hetzner PVCs were removed from
desired state after cutover.

This runbook moves Grocy application data from Hetzner local-path PVCs to OVH
Kimsufi local-path PVCs using VolSync `rsyncTLS`.

The two target PVCs are:

| Instance      | Source PVC                   | Source storage       | Destination PVC                  | Destination storage |
| ------------- | ---------------------------- | -------------------- | -------------------------------- | ------------------- |
| Grocy SF      | `grocy-sf/grocy-config`      | `local-path-hetzner` | `grocy-sf/grocy-config-ovh`      | `local-path-ovh`    |
| Grocy Vallejo | `grocy-vallejo/grocy-config` | `local-path-hetzner` | `grocy-vallejo/grocy-config-ovh` | `local-path-ovh`    |

Live paved result for Grocy SF:

- destination prep commit: `f49359fd3`
- pause and final sync commit: `85150b0fd`
- verification Job commit: `4d9e4546e`
- cutover commit: `f426f5727`
- final sync trigger: `final-20260520`
- VolSync source result: successful, 24.935819103s
- verification Job output: `ok`
- post-cutover pod: `grocy-66b6c66cd7-tpzhz` on `talos-kimsufi-worker-1`
- post-cutover app check: `/login` returned HTTP 200 inside the pod

Live paved result for Grocy Vallejo:

- destination prep commit: `59bd81c46`
- pause and final sync commit: `a7a363094`
- verification Job commit: `c335557cf`
- cutover commit: `a3f9aedae`
- final sync trigger: `final-20260520`
- VolSync destination result: successful, 13m17.143511483s
- source-side result: failed after the destination completed; see friction item 7
- verification Job output: `ok`
- post-cutover pod: `grocy-66b6c66cd7-q8vmr` on `talos-kimsufi-worker-1`
- post-cutover app check: `/login` returned HTTP 200 inside the pod

## Smoke-Test Findings

Disposable PVC test path:

- source PVC on `local-path-hetzner`
- destination PVC on `local-path-ovh`
- `ReplicationDestination` with `rsyncTLS.copyMethod: Direct`
- `ReplicationSource` with `rsyncTLS.copyMethod: Direct`
- source address set to the destination service DNS:
  `volsync-rsync-tls-dst-<destination-name>.<namespace>.svc`

Friction found while paving:

1. `local-path-hetzner` uses `allowedTopologies` of `region=hil`, and Kimsufi
   nodes also carry `region=hil`. A newly-created test source PVC must be
   explicitly consumed by a pod pinned to `zone=hil-dc1` if it needs to bind on
   the old VPS nodes. Existing Grocy source PVCs are already bound to
   `talos-vps-worker-1`, so this mainly affects tests and future PVC creation.
2. `rsyncTLS` movers must run with a non-zero UID unless privileged movers are
   enabled for the namespace. For Grocy, set both source and destination mover
   security context to UID/GID `1000`, matching the Grocy container's `PUID` and
   `PGID`.
3. If `ReplicationDestination.spec.rsyncTLS.keySecret` is omitted, VolSync
   generates a Secret named `volsync-rsync-tls-<destination-name>` and reports
   it in `.status.rsyncTLS.keySecret`. Use that name from the source CR instead
   of committing a plaintext PSK.
4. The installed CRD reports `.status.rsyncTLS.address`, but not the default
   port. Use port `8000`.
5. Grocy's database is `/config/data/grocy.db`. The image has PDO SQLite, not
   the `sqlite3` CLI or PHP `SQLite3` class. Use:

   ```sh
   php -r '$db=new PDO("sqlite:/config/data/grocy.db"); echo $db->query("PRAGMA integrity_check")->fetchColumn(), "\n";'
   ```

6. Immediately after pushing a new cluster-wide revision, Flux dependencies may
   briefly report stale `DependencyNotReady` statuses even though their own
   dependencies have already recovered. Do not bypass Flux. Reconcile the stale
   dependency, then reconcile the target again.
7. On Grocy Vallejo, the `ReplicationDestination` reported a successful receive
   while the `ReplicationSource` retried and eventually failed once the
   destination service had no active mover endpoint. Treat source success as the
   clean path, but the real cutover gate is destination-side completion plus a
   verifier Job against the destination PVC.

## Phase 0: Preconditions

Confirm VolSync and snapshot-controller are healthy:

```sh
kubectl -n flux-system get kustomization snapshot-controller-crds snapshot-controller volsync
kubectl -n volsync-system get helmrelease volsync
kubectl -n volsync-system get pods -o wide
```

Confirm the source PVC and pod:

```sh
kubectl -n grocy-sf get pvc grocy-config -o wide
kubectl -n grocy-sf get pod -o wide
kubectl -n grocy-vallejo get pvc grocy-config -o wide
kubectl -n grocy-vallejo get pod -o wide
```

The Grocy application deployment must use `strategy.type: Recreate`. It does in
`cluster/k8s/grocy/app-base/deployment.yaml`.

## Phase 1: Prepare Destination

For one instance, add an overlay-local `pvc.yaml` containing the permanent
destination PVC, and add a separate `volsync-migration.yaml` containing the
temporary `ReplicationDestination`.

Example permanent PVC for `grocy-sf`:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: grocy-config-ovh
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: local-path-ovh
  resources:
    requests:
      storage: 1Gi
```

Example temporary `ReplicationDestination` for `grocy-sf`:

```yaml
apiVersion: volsync.backube/v1alpha1
kind: ReplicationDestination
metadata:
  name: grocy-config-ovh-migration
spec:
  trigger:
    manual: prep-20260520
  rsyncTLS:
    destinationPVC: grocy-config-ovh
    copyMethod: Direct
    serviceType: ClusterIP
    moverSecurityContext:
      runAsUser: 1000
      runAsGroup: 1000
      fsGroup: 1000
      seccompProfile:
        type: RuntimeDefault
```

Add the file to that instance's `kustomization.yaml`, commit, push, reconcile,
then wait:

```sh
flux reconcile kustomization grocy-sf -n flux-system --with-source
kubectl -n grocy-sf wait --for=jsonpath='{.status.rsyncTLS.keySecret}' \
  replicationdestination/grocy-config-ovh-migration --timeout=120s
kubectl -n grocy-sf get replicationdestination grocy-config-ovh-migration \
  -o jsonpath='{.status.rsyncTLS.address}{"\n"}{.status.rsyncTLS.keySecret}{"\n"}'
```

Expected generated key Secret:

```text
volsync-rsync-tls-grocy-config-ovh-migration
```

## Phase 2: Pause And Sync

Add a temporary Kustomize patch that scales the Grocy application deployment to
zero. Do this in Git, not with `kubectl scale`, so Flux does not fight the
pause.

```yaml
patches:
  - target:
      kind: Deployment
      name: grocy
    patch: |-
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: grocy
      spec:
        replicas: 0
```

Add the `ReplicationSource`:

```yaml
apiVersion: volsync.backube/v1alpha1
kind: ReplicationSource
metadata:
  name: grocy-config-ovh-migration
spec:
  sourcePVC: grocy-config
  trigger:
    manual: final-20260520
  rsyncTLS:
    copyMethod: Direct
    keySecret: volsync-rsync-tls-grocy-config-ovh-migration
    address: volsync-rsync-tls-dst-grocy-config-ovh-migration.<namespace>.svc
    port: 8000
    moverSecurityContext:
      runAsUser: 1000
      runAsGroup: 1000
      fsGroup: 1000
      seccompProfile:
        type: RuntimeDefault
```

Replace `<namespace>` with `grocy-sf` or `grocy-vallejo`.

Commit, push, reconcile, then wait:

```sh
flux reconcile kustomization grocy-sf -n flux-system --with-source
kubectl -n grocy-sf rollout status deploy/grocy --timeout=120s
kubectl -n grocy-sf wait --for=jsonpath='{.status.lastManualSync}'=final-20260520 \
  replicationsource/grocy-config-ovh-migration --timeout=300s
kubectl -n grocy-sf get replicationsource grocy-config-ovh-migration \
  -o jsonpath='{.status.lastSyncTime}{"\n"}{.status.lastSyncDuration}{"\n"}{.status.conditions[*].message}{"\n"}'
```

Expected condition message after success:

```text
Waiting for manual trigger
```

## Phase 3: Verify Destination PVC

Run a one-shot verification Job against `grocy-config-ovh` before cutting over.
Use the Grocy image so the PHP runtime matches the app:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: grocy-config-ovh-verify
spec:
  template:
    spec:
      restartPolicy: Never
      nodeSelector:
        topology.kubernetes.io/zone: hil-ovh
      securityContext:
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: verifier
          image: lscr.io/linuxserver/grocy:v4.6.0-ls318
          command: ["/bin/sh", "-ceu"]
          args:
            - |
              test -f /config/data/grocy.db
              php -r '$db=new PDO("sqlite:/config/data/grocy.db"); echo $db->query("PRAGMA integrity_check")->fetchColumn(), "\n";' | grep -Fx ok
          volumeMounts:
            - name: config
              mountPath: /config
      volumes:
        - name: config
          persistentVolumeClaim:
            claimName: grocy-config-ovh
```

Apply through Git if the verification Job should be reproducible; otherwise an
imperative `kubectl apply -f <job.yaml>` is acceptable because it does not own
steady-state app configuration.

Wait and inspect logs:

```sh
kubectl -n grocy-sf wait --for=condition=complete job/grocy-config-ovh-verify --timeout=120s
kubectl -n grocy-sf logs job/grocy-config-ovh-verify
```

Expected output includes:

```text
ok
```

## Phase 4: Cut Over

Replace the pause patch with a deployment patch that points the app at OVH:

```yaml
patches:
  - target:
      kind: Deployment
      name: grocy
    patch: |-
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: grocy
      spec:
        replicas: 1
        template:
          spec:
            nodeSelector:
              topology.kubernetes.io/zone: hil-ovh
            volumes:
              - name: config
                persistentVolumeClaim:
                  claimName: grocy-config-ovh
```

Commit, push, reconcile, then verify:

```sh
flux reconcile kustomization grocy-sf -n flux-system --with-source
kubectl -n grocy-sf rollout status deploy/grocy --timeout=300s
kubectl -n grocy-sf get pod -o wide
```

The Grocy pod should run on `talos-kimsufi-worker-*` and mount
`grocy-config-ovh`.

Check the app:

```sh
kubectl -n grocy-sf logs deploy/grocy --tail=80
kubectl -n grocy-sf exec deploy/grocy -- /bin/sh -c \
  'php -r '\''$db=new PDO("sqlite:/config/data/grocy.db"); echo $db->query("PRAGMA integrity_check")->fetchColumn(), "\n";'\'''
```

Also verify the external route manually in a browser or with an authenticated
request if available.

## Rollback Before Cleanup

Until the old `grocy-config` PVC is deleted, rollback is:

1. change the deployment patch back to `claimName: grocy-config`;
2. change the node selector back to the old placement if needed;
3. reconcile.

After Grocy writes to `grocy-config-ovh`, rollback may lose those new writes
unless a reverse sync is performed first.

## Cleanup

After a soak period:

1. delete the temporary `ReplicationSource`;
2. delete the temporary `ReplicationDestination`;
3. delete the verification Job;
4. delete the old `grocy-config` PVC only after confirming no rollback is
   needed;
5. repeat the same runbook for the other Grocy instance.

Do not remove the old PVC in the same commit as the cutover.
