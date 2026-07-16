# Calendar

The operator's schedule — upcoming commitments, availability, and the recurring shape of
their week; a window into what they're doing and when. Two read paths; prefer the first:

- **Primary: haku-console's `google_calendar` MCP tools** (own Google OAuth — independent of
  the `google-access-token` secret): `list_events` (with `time_min`/`time_max`,
  `expand_recurring`, free-text `query`, per-`calendar_id`), `get_event`,
  `list_event_instances` — all auto-approved reads for authenticated agents. Reach it like the
  `gmail` server ([gmail.md](gmail.md)): in-session MCP tools in managed sessions, else
  `https://haku.allegedly.works/mcp` with the `haku-console-agent-api` bearer
  ([`mcp_over_http.md`](mcp_over_http.md)). `create_event` exists but is approval-gated.
- **Fallback: REST with the read-only Google token** `$TOK` ([README](README.md)), e.g. the
  next ~7–14 days:
  `curl -s -H "Authorization: Bearer $TOK" 'https://www.googleapis.com/calendar/v3/calendars/primary/events?timeMin=<now>&timeMax=<+14d>&singleEvents=true&orderBy=startTime'`.

Each event carries title, start/end, attendees, location, and an `htmlLink` (use it to link
the event in items). Calendar is most useful **cross-referenced** — with mail/Tana to tell
whether an event still stands or implies work, and as the "when/where" that reprioritizes
other items.

What to _do_ with it (prep gaps, conflicts, implied tasks) → the **calendar-prep** pass in
your procedures (`procedures/calendar_and_geo.md`, in your state).
