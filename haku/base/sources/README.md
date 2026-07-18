# Information sources

These document Haku's **information channels** — operator-owned sources it reads to
understand their life, plus Haku-owned channels such as its mailbox. The authority inventory
is canonical in `../instructions.md` → _Hard rules_; these guides document access mechanics.

**They are inputs and reference, never a checklist.** A run is not "execute source 1,
2, 3 … done." Reading these is **instrumental**: Haku reads them to build situational
awareness (what is the operator up to right now? what's forming? what's stuck?) and to
spot problems and opportunities — then it **reasons, researches, explores options, and
synthesizes** that into a value-ranked dashboard of recommended actions (see
`../instructions.md`). The synthesis is the job; the sources are how it gets the raw
material. Haku uses each however it's useful for that goal, combines them, and ignores
the ones that aren't relevant this run.

Each file says **what the channel conveys** and **how to access it**
(auth, API, query shape, operations, gotchas). They're starting points, not a closed set — Haku
grows its own techniques and records them in its state `memory/`.

## Sources

- [`gmail`](gmail.md), [`calendar`](calendar.md),
  [`drive`](drive.md), [`tasks`](tasks.md) — the Google surface. **Gmail and Calendar
  reads go primarily through haku-console's `gmail`/`google_calendar` MCP tools**
  (console-owned Google OAuth, auto-approved reads — see each guide; independent of the
  `google-access-token` secret). Drive and Tasks have no console tools yet and stay on the
  **one read-only token**, which also remains the Gmail/Calendar fallback. Fetch it once and
  reuse as `$TOK` (its scopes are all `.readonly`, so a write fails even if attempted):
  `TOK=$(kubectl -n haku-sandbox get secret google-access-token -o jsonpath='{.data.access_token}' | base64 -d)`,
  then call the REST APIs with `Authorization: Bearer $TOK` (curl goes through the
  egress proxy transparently). Other Google products work the same way when the token
  carries their scope (a 403 = scope/enablement gap → note it and move on).
- [`tana`](tana.md) — the operator's Tana, their primary brain; reached via
  haku-console's `tana-rw` MCP tools (read subset auto-approved, proxied like
  Gmail/Calendar/osm). The richest seam of intentions tracked nowhere else — a
  must-read source, not optional.
- [`plaid`](plaid.md) — financial transactions (read-only SQL via
  a `haku-sandbox` pod).
- [`coinbase`](coinbase.md) — crypto balances & trade fills (read-only CDP key
  reflected into `haku-sandbox`; JWT/ES256 auth). Coinbase isn't a Plaid
  institution, so this is the only read path for crypto holdings.
- [`cpap`](cpap.md) — the operator's CPAP sleep therapy data (AHI, leak, compliance);
  a git clone of the private `cpap-data` repo using your existing Forgejo login.
- [`grocy`](grocy.md) — the operator's household stock (expiring /
  below-minimum items, shopping suggestions); reached via haku-console's `grocy-sf`
  MCP tools (read subset auto-approved, proxied like Gmail/Calendar/osm/Tana).
- [`activitywatch`](activitywatch.md) — the operator's device activity: presence
  (at which computer, right now), focus, and per-day time-use; read-only via an
  Authentik two-step token mint. The prioritization signal the other sources lack.
- [`osm`](osm.md) — geocoding, routing, and place lookup via public OpenStreetMap
  APIs (Nominatim/OSRM/Overpass, wrapped by the `osmmcp` MCP server, proxied through
  haku-console like Gmail/Calendar). Reference, not an operator-owned channel — every
  tool auto-approves.
- [`mailbox`](mailbox.md) — **your own mailbox** (`haku@allegedly.works`): mail the
  operator sends directly to you (requests, context, forwards). Delivery is DMARC-gated
  to whitelisted senders at the server; access over JMAP with your Authentik mail JWT.
- [`ducktape`](ducktape.md) — the operator's recent repo work
  (always reachable; you have the checkout). The **cluster** is likewise a standing
  source — see `../instructions.md` → _How you reason_ (read-only diagnostics).

The MCP-server sources (`tana`, `grocy`, `osm`) share one transport how-to —
[`mcp_over_http.md`](mcp_over_http.md) (`fastmcp`, `curl` fallback, reading the
bearer); their own files carry only the URL, secret, tools, and gotchas. It's a
shared mechanic, not a channel.

## Techniques live elsewhere

Reusable, source-agnostic ways to be useful — inbox-like triage & cleanup, delegation
scans, opportunistic synthesis, quiet-run recon, … — are **not** here; they're your
**procedures**, in your state (`procedures/`, yours to
grow), applied situationally across whatever sources fit (illustrations, not a checklist;
invent your own). This directory is just the channels and how to read them.

Designed but **not yet wired** (don't attempt; note the gap if one appears):
PostScanMail (unopened mail) — blocked until it sits behind a read-only filter
facade; see `../../PLAN.md`.
