# Information sources

These document Haku's **information sources** — the operator-linked channels it reads
to understand what's going on in the operator's life and to find ways to help: their
inbox, calendar, Drive, Tana notes, recent repo work, the cluster, their finances.

**They are inputs and reference, never a checklist.** A run is not "execute source 1,
2, 3 … done." Reading these is **instrumental**: Haku reads them to build situational
awareness (what is the operator up to right now? what's forming? what's stuck?) and to
spot problems and opportunities — then it **reasons, researches, explores options, and
synthesizes** that into a value-ranked dashboard of recommended actions (see
`../instructions.md`). The synthesis is the job; the sources are how it gets the raw
material. Haku uses each however it's useful for that goal, combines them, and ignores
the ones that aren't relevant this run.

Each file says **what the source tells Haku about the operator** and **how to read it**
(auth, API, query shape, gotchas). They're starting points, not a closed set — Haku
grows its own techniques and records them in its state `memory/`.

## Sources (operator-linked channels)

- [`gmail`](gmail.md), [`calendar`](calendar.md),
  [`drive`](drive.md), [`tasks`](tasks.md) — the Google surface, all on **one
  read-only token**. Fetch it once and reuse as `$TOK` (its scopes are all
  `.readonly`, so a write fails even if attempted):
  `TOK=$(kubectl -n haku-sandbox get secret google-access-token -o jsonpath='{.data.access_token}' | base64 -d)`,
  then call the REST APIs with `Authorization: Bearer $TOK` (curl goes through the
  egress proxy transparently). Other Google products work the same way when the token
  carries their scope (a 403 = scope/enablement gap → note it and move on).
- [`tana`](tana.md) — the operator's Tana, their primary brain; reached
  via the `tana-mcp-ro` facade (`fastmcp`, or `curl` fallback). The richest seam of
  intentions tracked nowhere else — a must-read source, not optional.
- [`plaid`](plaid.md) — financial transactions (read-only SQL via
  a `haku-sandbox` pod).
- [`grocy`](grocy.md) — the operator's household stock (expiring /
  below-minimum items, shopping suggestions); reached via the grocy-sf MCP
  (`fastmcp`), read-only because the `haku` Grocy user has empty permissions.
- [`ducktape`](ducktape.md) — the operator's recent repo work
  (always reachable; you have the checkout). The **cluster** is likewise a standing
  source — see `../instructions.md` → _How you reason_ (read-only diagnostics).

The MCP-server sources (`tana`, `grocy`) share one transport how-to —
[`mcp_over_http.md`](mcp_over_http.md) (`fastmcp`, `curl` fallback, reading the
bearer); their own files carry only the URL, secret, tools, and gotchas. It's a
shared mechanic, not a channel.

## Techniques live elsewhere

Reusable, source-agnostic ways to be useful — inbox-like triage & cleanup, delegation
scans, opportunistic synthesis, quiet-run recon, … — are **not** here; they're example
**recipes** in [`../recipes.md`](../recipes.md), applied situationally across whatever
sources fit (illustrations, not a checklist; invent your own). This directory is just
the channels and how to read them.

Designed but **not yet wired** (don't attempt; note the gap if one appears):
PostScanMail (unopened mail) — blocked until it sits behind a read-only filter
facade; see `../../PLAN.md`.
