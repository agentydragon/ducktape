# Sync to Postgres

`export_sessions_bin sync` keeps a PostgreSQL database level with every session's events, until stopped. The first
cycle backfills; later cycles read what changed. Schema changes run through Alembic
([migrations/](../migrations/versions/0001_sessions.py)), applied at startup under an advisory lock.

```bash
SESSION_SYNC_DATABASE_URL=postgresql://user:pw@host/db \
  bb run //devinfra/claude/session_export:export_sessions_bin -- sync --credentials-file ~/.claude-session-export.json
```

`--once` runs a single cycle; `--interval` (default 300 s) and `--workers` (default 3) tune the loop. Give the
credential file one owning process, as for `export` ([README](../README.md)): a second process refreshing it
invalidates the first's grant.

## Schema

`sessions` holds one row per session: `session_id` (`session_<x>`), `title`, `status`, `created_at`, `updated_at`,
`last_event_at`, `synced_last_event_at` and `raw`, the list item as sent (model, repo, branches and the rest stay
there).

`events` holds one row per event, keyed by `(session_id, sequence_num)`:

- `sequence_num` is the order. `created_at` is not monotonic within a session (421 inversions in 5.2 M events).
- `event_id` is a `uuid` with no index: the API repeats ids across sessions (3,492 in the archive).
- `event_type` and `source` are plain `text`. The vocabularies are open (20 event types, one of them seen once).
- `received_at`, `processing_at` and `processed_at` are the worker queue's stamps, present on about a fifth of
  events. NULL means the event has not reached that stage, or never passes through the queue: only events with
  `source: client` do (a user's message, a queued notification, a control response), never the worker's own.
- `payload` is `jsonb`, in `lz4`-compressed TOAST.
- `device_attestation_status` and `sent_by_account_id` are not stored: they are `DEVICE_ATTESTATION_STATUS_UNSPECIFIED`
  and null on every event observed, and an event that differs makes the sync raise rather than lose the value.
- The one index besides the key is `(event_type, created_at)`. Promoted columns (`subtype`, `parent_tool_use_id`) can
  be added later as virtual generated columns, which PostgreSQL 18 adds without rewriting the table; they cannot be
  indexed, so an expression index would carry any query that needs one.

## A cycle

1. List every session, requesting routine runs with `include_trigger_sessions=true`, and upsert its metadata. The
   whole list is read each time (about 16 requests per 1,500 sessions) rather than stopping at the first unchanged
   session: a cycle that died partway leaves older sessions behind a newer one that is level.
2. A session is behind when `synced_last_event_at` differs from the `last_event_at` just listed.
3. For each behind session, read events after the resume point and store them page by page. The resume point is the
   newest stored `sequence_num`, or just before the earliest event that had `received_at` but no `processed_at` when
   stored: the stamps do move on after an event is first read, and re-reading it refreshes them.
   `SessionsApi.iter_event_pages` raises on a page with a gap in `sequence_num` before yielding it, so none is
   stored: `resume_after` reads the newest stored number, and an event stored past a gap would never be fetched.
4. Set `synced_last_event_at` to the value listed in step 1, not a newer one: events arriving during the pass leave the
   session behind for the next cycle. A crash before this step leaves it behind, and the next cycle resumes after
   the last stored event.

`last_event_at > synced_last_event_at` is the lag of one session.

## Live following

`serve` adds event streams between the cycles ([live.py](../live.py)); `sync` only polls. The cycle stays the source
of truth and the streams only shorten the delay.

- **Event streams.** A session that is not archived and had an event within `LIVE_WINDOW_SECONDS` gets an open
  `events/stream` from its stored position, up to `LIVE_STREAMS` sessions, newest first. The first connection
  starts with a page catch-up so the position is exact; each pushed event is stored as it arrives, and a closed or
  silent stream is reopened from the stored position.
- **Discovery.** Every 30 s the newest `LIVE_STREAMS` sessions are listed (one request) and upserted, and the followed
  set is re-derived from the store: a new session is followed within about 30 s, and one that was archived or has
  aged out of the window is dropped. A finished cycle triggers the same pass at once. This is the floor: it needs
  nothing but the list route, and it is what the web client does itself when its watch is off.
- **Session watch.** One `sessions/watch` stream pushes each session change: the change is upserted and the followed
  set re-derived at once, so a new session is followed from its first event ([api.md](api.md) § Session watch: the
  request needs a platform header). The page shows "watch connected", or "not connected" with the reason; discovery
  covers meanwhile and after a gap. Every connection takes a fresh resume token, since the server answers one older
  than about four minutes with a watch that stays open and delivers nothing, and after a reconnect the last checkpoint
  can already be two minutes old.

Rules that keep the mirror exact:

- An event is stored only when its `sequence_num` is at most one past the stored position. A frame beyond that, a
  `catch_up_truncated` frame or a 410 sends the session back to paging from its stored position before its stream is
  reopened: `resume_after` reads the newest stored number, so an event stored past a gap would never be fetched.
- A stream pushes a client-sent event without its worker stamps and does not send it again when they change; a
  `delivery_update` frame says one did. The follower looks the event up by its `event_id` in the store, reads it from
  the events route (`SessionsApi.read_event`) and stores it again, which refreshes the stamps. It does not read the
  frame's status or copy its `timestamp`, which is not the stored stamp. An update for an event that is not stored is
  logged as a warning and skipped.
- Streams do not move `synced_last_event_at`. A session they touched is behind until the next cycle reads it, which
  finds nothing new and marks it level.
- A failing source (discovery, the watch, or one session's stream) retries with jittered backoff from 1 s to 5 min and never
  ends the cycle. The page lists each failing source with the reason (HTTP status, route and the API's error body)
  until it works again, and the log carries the same line. A follower that stops altogether is reported too.
  `LIVE_STREAMS=0` turns following off.

The wire contract of the streams is in [api.md](api.md) § Event stream and § Session watch.

## `payload` is `jsonb`; `json` is the open alternative

`jsonb` rejects U+0000, so the sync rewrites each NUL character to U+2400 (`␀`) and logs the count: 151 of the 5.2 M
archived events hold one. Nothing else is altered: all 167,298 payloads of a random 60-session sample came back
equal to what the API sent, apart from that rewrite, and none had duplicate keys. Lone surrogate escapes are refused
by PostgreSQL and by the client's JSON parser alike; none occur in the archive, so one would stop the sync at parsing.

`json` keeps every byte and accepts NUL, but a query that extracts from such a row errors, even for another key, and
so does a cast to `jsonb`; it has no `@>` and no GIN. On the same sample, measured on PostgreSQL 16 (warm cache, one run):

|                                         | `jsonb` | `json` |
| --------------------------------------- | ------- | ------ |
| Stored, heap + TOAST + key (167 k rows) | 272 MB  | 259 MB |
| `payload->>'subtype'` over the sample   | 132 ms  | 209 ms |
| `payload#>>'{message,model}'`           | 145 ms  | 272 ms |

`lz4` and `pglz` stored the same size; `lz4` loaded about 25% faster. Extrapolating the sample to the archive gives
about 8.5 GB, of which 0.6 GB is the key, before the `(event_type, created_at)` index, which was not measured. It grows
about 0.5 GB a week at the recent 200–460 k events a week. The sample is 3.2% of the archive, so these are estimates.
