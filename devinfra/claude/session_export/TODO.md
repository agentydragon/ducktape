# TODO

## Decide `events.payload`: `json` or `jsonb`

Ships as `jsonb` with a NUL rewrite ([docs/sync.md](docs/sync.md) § `payload`). The choice is open: check on a fuller
sample than the 3.2% measured (size, extraction timings, how the 151 NUL rows would be filtered out of `json`
queries) and decide whether byte-exact payloads outweigh containment queries and a clean scan.

Switching is one column type in `store.py` and `migrations/versions/0001_sessions.py`, and dropping `dumps_jsonb`'s
rewrite; deployed databases are disposable.

## Observe live following against the real API

`live.py` follows the two streams as the web client's code describes them ([docs/api.md](docs/api.md) § Event stream
and session watch); none of it has run against Anthropic. With a paired credential, check that:

- the bearer token opens both streams (the list and events routes accept it; the streams are untested), and the
  watch does not also need `anthropic-client-platform: web_claude_ai`;
- a quiet stream is kept alive within `STREAM_IDLE_TIMEOUT` (35 s, the web client's own limit) rather than reconnecting
  every time, and how long the watch stays silent;
- a `changed` frame arrives when a session gets events, and carries a complete session (the upsert needs `title`,
  `status` and the timestamps);
- the worker stamps (`received_at`, `processing_at`, `processed_at`) reach a stream event unset or complete, and a
  frame is re-sent when they change.

Delete this entry once each is confirmed, correcting the docs where it is not.
