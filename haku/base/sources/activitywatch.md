# ActivityWatch — operator presence, focus, and time-use

**What it tells Haku:** whether the operator is at a computer right now (and which one),
what has focus (app/window/browser tab), and how today's time was actually spent. This is
the prioritization signal the other sources lack: rank against what he's doing _now_,
notice "3h in X today → that project is hot," and stop nudging things he's clearly on.

**Access contract:** read-only by construction — the cluster exposes only GET plus
`POST /api/0/query/` through a read-only proxy; the write API is unreachable from agents.
Credential: the `activitywatch-haku-client-credentials` secret in `haku-sandbox`. Auth is
a two-step Authentik mint (source JWT → proxy bearer); the generic recipe, secret fields,
and API gotchas live with the infrastructure doc, <../../../cluster/docs/activitywatch/README.md>
(the same flow serves every agent, each with its own reflected secret). Haku maintains its
own living helper and usage procedure in its state.

**Privacy contract:** window titles and URLs are as sensitive as mail bodies — reference
(app, domain, duration), never dump raw event lists into anything surfaced or committed.
