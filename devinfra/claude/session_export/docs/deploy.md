# Running the sync in the cluster

[`cluster/cdk8s/claude_session_sync`](../../../../cluster/cdk8s/claude_session_sync/app.py) deploys `sync` as one
replica in the `claude-session-sync` namespace, writing to a two-instance CNPG database (`claude-session-sync-db`)
in the same zone. The OAuth credential lives on the `claude-session-sync-data` volume at `/data/credentials.json`
and is minted inside the running container: the pod starts unpaired, logs that it is waiting, and picks the file up
within seconds of `pair` writing it.

## Pair

The browser must reach the loopback callback of `pair` inside the pod, so forward its port and run `pair` there:

```bash
kubectl -n claude-session-sync port-forward deploy/claude-session-sync 54545:54545 &
kubectl -n claude-session-sync exec -it deploy/claude-session-sync -- \
  /devinfra/claude/session_export/export_sessions_image_bin pair --credentials-file /data/credentials.json
```

Open the printed URL in a browser signed in to the account and approve. `kubectl -n claude-session-sync logs
deploy/claude-session-sync` then shows the first cycle backfilling; sessions are read three at a time, and a large
session logs its progress.

## Pair again

A running sync holds the credential in memory and rewrites the file on each refresh, so pairing over it would be
overwritten. Delete the file and restart the pod first:

```bash
kubectl -n claude-session-sync exec deploy/claude-session-sync -- rm /data/credentials.json
kubectl -n claude-session-sync rollout restart deploy/claude-session-sync
```

Then pair as above. The sync also stops with an error, and the pod restarts, when a refresh is refused (the grant
was revoked or lost); pair again then.

## Look

```bash
kubectl -n claude-session-sync exec -it \
  "$(kubectl -n claude-session-sync get pod -l cnpg.io/cluster=claude-session-sync-db,cnpg.io/instanceRole=primary -o name)" \
  -c postgres -- psql -d claude_sessions
```

`SELECT session_id, last_event_at - synced_last_event_at AS lag FROM sessions` lists the sessions the sync is behind
on ([sync.md](sync.md) § A cycle).

## Not covered

The database replicates across two nodes but has no WAL archive or base backups, and nothing alerts when the sync
stalls beyond the cluster's crash-loop alerts.
