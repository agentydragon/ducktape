# Recorder cutover to Postgres

Moves Home Assistant's recorder from SQLite on the config PVC (`/config/home-assistant_v2.db`)
to the `home-assistant-db` CNPG Cluster without suspending Flux. The live instance stays on SQLite
until the last step; the cost is losing what it records between the final copy and its restart on
Postgres. Delete this plan once the cutover has run.

1. Merge the Cluster (`cluster/cdk8s/home_assistant/db.py`). Home Assistant is untouched.
2. Create the schema: apply the `recorder-schema` Job below. It runs the same image version as the
   live Deployment with a config holding only `recorder:`, so the recorder creates its tables in
   Postgres. Once
   `kubectl cnpg psql home-assistant-db -n home-assistant -- homeassistant -c 'select max(schema_version) from schema_changes'`
   returns a row, delete the Job.
3. Rehearse the load: apply the `recorder-load` Job. It takes an online backup of the live SQLite
   file (consistent while Home Assistant writes) and loads it data-only into that schema
   (`truncate`, so re-running is safe; `reset sequences` moves the ids past the copied rows). If
   foreign keys reject the load order, `disable triggers` needs a superuser, which the
   `homeassistant` owner is not: decide then.
4. Cut over: delete and re-apply `recorder-load`, and as soon as it finishes merge the change that
   sets `recorder: db_url: !env_var RECORDER_DB_URL` (from the Cluster's `uri`) and the retention.
   Flux restarts Home Assistant on Postgres. Check that the History panel shows days before the
   cutover, then delete the Job.

Fallback at any step: skip 2–4's loads and merge the recorder change; history starts fresh, losing
the ~10 days SQLite holds. `.storage/` (devices, entities, zones, users) stays on the PVC either
way, as does the old SQLite file until someone deletes it.

Both Jobs pin the live image version: when the Deployment's Home Assistant version changes before
the cutover, update them and redo step 2, because the load must match the schema version.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: recorder-schema
  namespace: home-assistant
data:
  configuration.yaml: |
    recorder:
      db_url: !env_var RECORDER_DB_URL
---
apiVersion: batch/v1
kind: Job
metadata:
  name: recorder-schema
  namespace: home-assistant
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      nodeSelector:
        kubernetes.io/hostname: optiplex
      volumes:
        - name: seed
          configMap:
            name: recorder-schema
        - name: config
          emptyDir: {}
      containers:
        - name: home-assistant
          image: ghcr.io/home-assistant/home-assistant:2026.9.2
          env:
            - name: RECORDER_DB_URL
              valueFrom:
                secretKeyRef:
                  name: home-assistant-db-app
                  key: uri
          volumeMounts:
            - name: seed
              mountPath: /seed
            - name: config
              mountPath: /scratch
          command:
            - sh
            - -ec
            - cp /seed/configuration.yaml /scratch/ && exec python3 -m homeassistant --config /scratch
---
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
        - name: work
          emptyDir: {}
      initContainers:
        - name: snapshot
          image: ghcr.io/home-assistant/home-assistant:2026.9.2
          volumeMounts:
            - name: config
              mountPath: /config
            - name: work
              mountPath: /work
          command:
            - python3
            - -c
            - |
              import sqlite3
              src = sqlite3.connect("file:/config/home-assistant_v2.db?mode=ro", uri=True)
              src.backup(sqlite3.connect("/work/ha.db"))
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
            - name: work
              mountPath: /work
          command:
            - sh
            - -ec
            - |
              cat > /work/load <<EOF
              LOAD DATABASE FROM sqlite:///work/ha.db INTO ${PG_URI}
              WITH data only, truncate, reset sequences, batch rows = 10000
              EXCLUDING TABLE NAMES LIKE 'sqlite_stat%';
              EOF
              pgloader /work/load
```

The snapshot mounts the PVC read-write but only reads it: SQLite's read-only open of a WAL
database still needs the `-shm` file, which a read-only mount refuses.
