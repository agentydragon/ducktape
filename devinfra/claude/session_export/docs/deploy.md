# Running the sync in the cluster

[`cluster/cdk8s/claude_session_sync`](../../../../cluster/cdk8s/claude_session_sync/app.py) deploys `serve`
([serve.md](serve.md)) as one replica in the `claude-session-sync` namespace, writing to a two-instance CNPG database
(`claude-session-sync-db`) in the same zone. The page is at <https://claude-session-sync.allegedly.works>, behind an
Authentik login that admits only the owner. The OAuth credential lives on the `claude-session-sync-data` volume at
`/data/credentials.json`.

## First deployment

1. `tf/gitops/sso-providers/provider_claude_session_sync.tf` creates the Authentik provider and application and the
   `claude-session-sync-oidc` Secret, which Reflector copies into the namespace. The pod cannot start until that
   Secret exists (`CreateContainerConfigError`), so let the Terraform reconcile finish first.
2. The image is published by the first push to `devel` after the change merges; until Flux's image automation
   commits the tag, the pod is in `ImagePullBackOff`.
3. Open the page, sign in, and pair: **Start pairing**, approve in Claude, paste back the address the browser lands
   on. The first cycle then backfills; a large session logs its progress
   (`kubectl -n claude-session-sync logs deploy/claude-session-sync`).

Pairing again from the page replaces the grant; the running sync switches to it. A grant that was revoked shows up as
a failed cycle on the page, and pairing again fixes it.

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
