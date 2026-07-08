# cluster/k8s/haku/console — Haku console deployment

Manifests for `haku/console/` (see that directory's README for the app itself). Deploy
notes here cover only what's specific to running it in-cluster.

## One-time bootstrap: the native `google` MCP server

The console's native `google` MCP server (`haku/console/google_tools.py` — calendar event
creation, batch Gmail thread label changes, Gmail draft creation, all behind the ordinary
operator-approval queue) needs a one-time browser consent for its `console_google` Airlock
provider before it's functional. The console pod itself starts fine either way (the token
volume is `optional: true`); until consent happens, that one MCP server's tools error on
invocation instead of running.

0. No Google console change needed: `console_google` reuses the `google` provider's
   already-registered redirect URI (`…/oauth/callback/google`) on the same OAuth client —
   the callback resolves the provider from OAuth `state`, not the path.
1. Visit `https://airlock.allegedly.works/oauth/authorize/console_google` and consent as the
   target Google account. Airlock's callback writes `console-google-tokens` (refresh) and
   `console-google-access-token` (access-only) into the `airlock` namespace; the refresh
   loop keeps the access token fresh thereafter.
2. ESO mirrors `console-google-access-token` into the `haku-console` namespace within ~1m.
   No restart needed — the console re-reads the mounted token via google-auth's
   `refresh_handler` on each rotation, same as `gmail-labeling`
   (<../../agents/gmail-labeling/README.md>).

Provider config: `agents/airlock/config.yaml` (`console_google`); the same Google OAuth
client as `google`/`gmail_modify` is reused via `CONSOLE_GOOGLE_CLIENT_ID/SECRET`
(`agents/airlock/deployment.yaml`).

Scopes: `calendar.events`, `gmail.modify`, `gmail.compose` — kept in one grant since one
server consumes all three; deliberately its own provider (not reusing `google` or
`gmail_modify`) so no other consumer's token is ever upgraded to this scope set. See
`haku/docs/security.md` for the enforcement-inventory entry.
