# ActivityWatch — operator presence, focus, and time-use

**What it tells Haku:** whether the operator is at a computer right now (and which one),
what has focus (app/window/browser tab), and how today's time was actually spent. This is
the prioritization signal the other sources lack: rank against what he's doing _now_,
notice "3h in X today → that project is hot," and stop nudging things he's clearly on.

**Infrastructure + design:** <../../../cluster/docs/activitywatch.md> (query server,
Syncthing sync topology, the read-only nginx proxy, the Authentik route, per-agent
service accounts). The cluster serves a **read-only** surface: GET plus
`POST /api/0/query/` only — the write API is not reachable from agents, by construction.

## How to read it

Credential: the `activitywatch-haku-client-credentials` secret in `haku-sandbox`
(fields: `activitywatch_url`, `token_url`, `client_id`, `username`, `password`,
`source_scopes`, `proxy_client_id`, `proxy_scopes`).

Auth is a two-step Authentik mint (verified 2026-07-07):

1. **Source JWT** — `POST $token_url` with `grant_type=client_credentials`,
   `client_id=$client_id`, `username=$username`, `password=$password`,
   `scope=$source_scopes` → `access_token` (1h expiry).
2. **Proxy bearer** — `POST $token_url` with `grant_type=client_credentials`,
   `client_id=$proxy_client_id`, `scope=$proxy_scopes`,
   `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`,
   `client_assertion=<source JWT>` → the token that `$activitywatch_url` accepts as
   `Authorization: Bearer`.

Gotchas: `POST /api/0/query` requires the **trailing slash** (`/api/0/query/` — nginx
301s otherwise and the redirected POST degrades to GET); transient TLS connection resets
occur (~1/20 calls) — retry once; bucket `last_updated` is always `null` on this server —
get recency from each bucket's newest event.

Haku's maintained helper (token caching, canned presence/time-use queries, all gotchas
baked in) lives in its state: `tools/aw.py` + `procedures/operator_status.md`.

**Privacy contract:** window titles and URLs are as sensitive as mail bodies — reference
(app, domain, duration), never dump raw event lists into anything surfaced or committed.
