# TODO

## Decide `events.payload`: `json` or `jsonb`

Ships as `jsonb` with a NUL rewrite ([docs/sync.md](docs/sync.md) § `payload`). The choice is open: check on a fuller
sample than the 3.2% measured (size, extraction timings, how the 151 NUL rows would be filtered out of `json`
queries) and decide whether byte-exact payloads outweigh containment queries and a clean scan.

Switching is one column type in `store.py` and `migrations/versions/0001_sessions.py`, and dropping `dumps_jsonb`'s
rewrite; deployed databases are disposable.

## Observe live following against the real API

Observed so far: the bearer token opens the event stream, which sends a frame with no `event:` name as it opens
([docs/api.md](docs/api.md) § Event stream). Still to check on a running session:

- `client_event` frames arrive and store cleanly (the page's "last event" moves and no page read follows the connect);
- a quiet stream stays open past `STREAM_IDLE_TIMEOUT` (35 s, the web client's own limit) instead of reconnecting
  each time, which says how often the keepalive comes;
- the worker stamps (`received_at`, `processing_at`, `processed_at`) reach a stream event unset or complete, and a
  frame is re-sent when they change;
- whether the first-party API host offers any feed of session changes (the web client's `sessions/watch` answers
  404), which would replace the 30 s discovery poll.

Delete this entry once each is confirmed, correcting the docs where it is not.
