# TODO

## Decide `events.payload`: `json` or `jsonb`

Ships as `jsonb` with a NUL rewrite ([docs/sync.md](docs/sync.md) § `payload`). The choice is open: check on a fuller
sample than the 3.2% measured (size, extraction timings, how the 151 NUL rows would be filtered out of `json`
queries) and decide whether byte-exact payloads outweigh containment queries and a clean scan.

Switching is one column type in `store.py` and `migrations/versions/0001_sessions.py`, and dropping `dumps_jsonb`'s
rewrite; deployed databases are disposable.

## Observe live following against the real API

Observed so far ([docs/api.md](docs/api.md) § Event stream, § Session watch): the bearer token opens the event stream
and the session watch (with its platform header), and a running session's stream sends `client_event` frames. Still to
check, with `export_sessions_bin probe --session ID --listen-seconds 60` (it counts every frame by name and data
shape):

- on the deployed sync, the page's "last event" moves while a session runs and no page read follows the connect;
- a quiet stream stays open past `STREAM_IDLE_TIMEOUT` (35 s, the web client's own limit) instead of reconnecting
  each time: the probe says "closed by the server" or "open at the end", and the count of unnamed frames is the
  keepalive cadence;
- the worker stamps (`received_at`, `processing_at`, `processed_at`): the first four `client_event` frames observed
  carry none, and an event stored from such a frame keeps NULL stamps, because `resume_after` re-reads only events
  with `received_at` set and `processed_at` unset. Whether the server sends a frame again when the stamps change
  shows as "id(s) sent again" while a message is sent; if it never does, decide whether the cycle should re-read
  recent events.

Delete this entry once each is confirmed, correcting the docs where it is not.
