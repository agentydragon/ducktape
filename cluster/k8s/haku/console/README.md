# cluster/k8s/haku/console — Haku console deployment

Manifests for `haku/console/` (see that directory's README for the app itself). Deploy
notes here cover only what's specific to running it in-cluster.

## One-time bootstrap: the in-process `gmail` + `google_calendar` MCP servers

The console's two in-process MCP servers — `gmail` (`haku/console/tools/gmail.py` — Gmail
reads mirroring the REST API, draft creation, thread-label changes, label CRUD) and
`google_calendar` (`haku/console/tools/google_calendar.py` — calendar event creation), both behind the
ordinary operator-approval queue — are both built from the single `haku_console_google` Airlock
token and need a one-time browser consent for its provider before they're functional. The
console pod itself starts fine either way (the token volume is `optional: true`); until consent
happens, those servers' tools error on invocation instead of running.

0. No Google console change needed: `haku_console_google` reuses the `google` provider's
   already-registered redirect URI (`…/oauth/callback/google`) on the same OAuth client —
   the callback resolves the provider from OAuth `state`, not the path.
1. Visit `https://airlock.allegedly.works/oauth/authorize/haku_console_google` and consent
   as the target Google account. Airlock's callback writes `haku-console-google-tokens`
   (refresh) and `haku-console-google-access-token` (access-only) into the `airlock`
   namespace; the refresh loop keeps the access token fresh thereafter.
2. ESO mirrors `haku-console-google-access-token` into the `haku-console` namespace within
   ~1m. No restart needed — the console re-reads the mounted token via google-auth's
   `refresh_handler` on each rotation, same as `gmail-labeling`
   (<../../agents/gmail-labeling/README.md>).

Provider config: `agents/airlock/config.yaml` (`haku_console_google`); the same Google
OAuth client as `google`/`gmail_modify` is reused via
`HAKU_CONSOLE_GOOGLE_CLIENT_ID/SECRET` (`agents/airlock/deployment.yaml`).

Scopes: `calendar.events`, `gmail.modify`, `gmail.compose`, plus every read-only scope the
`google` provider carries (`gmail.readonly`, `drive.readonly`, `drive.activity.readonly`,
`calendar.readonly`, `tasks.readonly`, `contacts.readonly`, `documents.readonly`,
`spreadsheets.readonly`, `presentations.readonly`, `youtube.readonly`) — kept in one grant
since the console's `gmail` and `google_calendar` servers both consume it, and carrying the
read scopes means a future haku-console read feature outside Gmail (Drive/Docs/…) needs no
second consent round-trip. Deliberately its own provider (not reusing `google` or
`gmail_modify`) so no other consumer's token is ever upgraded to this scope set. See
`haku/docs/security.md` for the enforcement-inventory entry.

## One-time bootstrap: `kubectl-passthrough-mcp` (cluster-admin, operator-linked)

The `kubectl-passthrough-mcp` MCP server entry (config.yaml — `pods_*`, `resources_*`,
`nodes_*`, `events_list`, `configuration_view`) uses `operator_oauth`, the same
per-operator browser-linked mechanism as `grocy-sf`/`tana-rw`: the operator connects once
from the console's Access tab (⚙ → Access → Connect next to `kubectl-passthrough-mcp`),
which runs Authentik's PKCE flow against `kubectl-passthrough-mcp`'s own OAuth2
application and stores the association in the console's Postgres database — no static
token, no secret to mount.

Unlike `grocy-sf`/`tana-rw`, this server forwards the connecting operator's own token
straight to kube-apiserver (`cluster_auth_mode = passthrough` in
`agents/kubectl-passthrough-mcp/`) rather than acting through a scoped service credential
of its own — the operator's real permissions apply, via the
`oidc-ksbx-agentydragon-admin` `ClusterRoleBinding`
(`agents/kubectl-passthrough-mcp/app/clusterrolebinding-agentydragon-admin.yaml`, cluster-admin).
So every tool call here runs with full cluster-admin once approved; the operator-approval
click in trusted console chrome is the only gate. See `haku/docs/security.md` for the
enforcement-inventory entry.
