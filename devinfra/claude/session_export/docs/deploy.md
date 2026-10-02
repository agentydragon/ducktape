# Running the sync in the cluster

[`cluster/cdk8s/claude_session_sync`](../../../../cluster/cdk8s/claude_session_sync/app.py) deploys two web replicas
and one private control replica in the `claude-session-sync` namespace, writing to a two-instance CNPG database
(`claude-session-sync-db`) in the same zone. The page is at <https://claude-session-sync.allegedly.works>, behind an
Authentik login that admits only the owner. Web Pods serve the login-protected page and proxy pairing/status/sync
actions to the private control Service. Cilium admits that Service only from web Pods. The control Pod alone mounts
`claude-session-sync-data` at `/data/credentials.json` and owns the rotating OAuth grant and sync loop.

## First deployment

1. `tf/gitops/sso-providers/provider_claude_session_sync.tf` creates the Authentik provider and application and the
   `claude-session-sync-oidc` Secret, which Reflector copies into the namespace. Web Pods cannot start until that
   Secret exists (`CreateContainerConfigError`), so let the Terraform reconcile finish first.
2. The image is published by the first push to `devel` after the change merges; until Flux's image automation
   commits the tag, the pod is in `ImagePullBackOff`.
3. Open the page, sign in, and pair: **Start pairing**, approve in Claude, paste back the address the browser lands
   on. The first cycle then backfills; a large session logs its progress
   (`kubectl -n claude-session-sync logs deploy/control`).

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

## Probe the API with the deployed credential

`probe` tries the session watch and, with `--session`, one session's event stream against the real API. It only reads
and never refreshes the token, so it is safe beside the control Pod (a refresh rotates the token it owns); it
stops if the access token has lapsed. It prints the status of each request and, for a stream, its frames counted by
name and data shape (keys only, never values), the ids sent more than once, and whether the server ended it. Findings so far: [api.md](api.md) § Session watch.

```bash
kubectl -n claude-session-sync exec deploy/control -- cat /data/credentials.json > /tmp/claude-credential.json
bb run //devinfra/claude/session_export:export_sessions_bin -- probe --credentials-file /tmp/claude-credential.json \
  --session session_<id> --listen-seconds 60
rm /tmp/claude-credential.json
```

## Not covered

The database replicates across two nodes but has no WAL archive or base backups, and nothing alerts when the sync
stalls beyond the cluster's crash-loop alerts.
