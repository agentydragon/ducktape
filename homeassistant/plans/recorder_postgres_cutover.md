# Recorder cutover to Postgres

One-time move of Home Assistant's recorder from SQLite on the config PVC
(`/config/home-assistant_v2.db`) to the `home-assistant-db` CNPG Cluster. Delete this plan once
the cutover has run.

Merging the PR that adds `recorder: db_url` is the cutover: Flux creates the Cluster, then
restarts Home Assistant on an empty Postgres. Copying the old history requires Home Assistant
to create its schema in Postgres first, at the same version as the SQLite file (2026.9.2 on both
sides), so the copy is data-only into that schema.

## Fallback: start fresh

The SQLite file holds only the default 10 days. Skipping the copy (merge, wait for Home Assistant
to come up) loses those 10 days and nothing else; `.storage/` (devices, entities, zones, users)
stays on the PVC either way. The old file stays on the PVC until someone deletes it.

## Rehearsal (before the window)

Copy the live file out (`kubectl -n home-assistant cp <pod>:/config/home-assistant_v2.db ./ha.db`;
copying a live SQLite file can catch a torn write, which is fine for a rehearsal). Run
`ghcr.io/home-assistant/home-assistant:2026.9.2` against a scratch Postgres with
`recorder: db_url` set until `schema_changes` has a row, stop it, then run the `pgloader` load
below against the scratch database. The rehearsal settles whether foreign keys need
`disable triggers` (that requires a superuser, which the `homeassistant` owner is not).

## Window (about 15 minutes of Home Assistant downtime)

1. `flux suspend kustomization home-assistant -n ducktape-flux`, then merge the PR. Nothing
   changes in the cluster yet.
2. `flux resume kustomization home-assistant -n ducktape-flux`. Flux creates the Cluster.
   Home Assistant waits in `CreateContainerConfigError` until CNPG writes `home-assistant-db-app`,
   then starts and creates its schema.
3. Once `kubectl cnpg psql home-assistant-db -n home-assistant -- homeassistant -c 'select max(schema_version) from schema_changes'`
   returns a row, run `flux suspend kustomization home-assistant -n ducktape-flux` and
   `kubectl -n home-assistant scale deploy/home-assistant --replicas=0`.
4. Apply the load Job below and follow `kubectl -n home-assistant logs -f job/recorder-load`.
   `truncate` drops the minutes Home Assistant wrote in step 2; `reset sequences` moves the
   id sequences past the copied rows.
5. `flux resume kustomization home-assistant -n ducktape-flux` (it restores the replica count),
   then check that the History panel shows days before the cutover. Delete the Job.

If step 4 fails, rerunning it is safe (`truncate`); to give up, resume as in step 5 and the
recorder carries on from an empty or partial history.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: recorder-load
  namespace: home-assistant
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      nodeSelector:
        kubernetes.io/hostname: optiplex
      volumes:
        - name: config
          persistentVolumeClaim:
            claimName: home-assistant-config-piraeus
            readOnly: true
        - name: work
          emptyDir: {}
      containers:
        - name: pgloader
          image: ghcr.io/dimitri/pgloader:v3.6.9
          env:
            - name: PG_URI
              valueFrom:
                secretKeyRef:
                  name: home-assistant-db-app
                  key: uri
          volumeMounts:
            - name: config
              mountPath: /config
              readOnly: true
            - name: work
              mountPath: /work
          command:
            - sh
            - -ec
            - |
              cp /config/home-assistant_v2.db /work/ha.db
              cat > /work/load <<EOF
              LOAD DATABASE FROM sqlite:///work/ha.db INTO ${PG_URI}
              WITH data only, truncate, reset sequences, batch rows = 10000
              EXCLUDING TABLE NAMES LIKE 'sqlite_stat%';
              EOF
              pgloader /work/load
```
