# TODO

## Decide `events.payload`: `json` or `jsonb`

Ships as `jsonb` with a NUL rewrite ([docs/sync.md](docs/sync.md) § `payload`). The choice is open: check on a fuller
sample than the 3.2% measured (size, extraction timings, how the 151 NUL rows would be filtered out of `json`
queries) and decide whether byte-exact payloads outweigh containment queries and a clean scan.

Switching is one column type in `store.py` and `migrations/versions/0001_sessions.py`, and dropping `dumps_jsonb`'s
rewrite; deployed databases are disposable.

## Streamed client events keep NULL worker stamps

A live `client_event` frame carries no stamps: they follow as `delivery_update` frames ([docs/api.md](docs/api.md)
§ Event stream), which the sync ignores. An event stored from a stream frame therefore has NULL stamps, is not "in
flight" for `resume_after` (that needs `received_at`), and nothing re-reads it: for every client event first seen on a
stream (a user's message, a queued notification) the mirror differs from a page read.

Two ways to close it. Apply `delivery_update` by re-reading the event by `event_id` (the frame's `timestamp` is not
the stored stamp, so it cannot be copied). Or treat a stored client event without `processed_at` as in flight, which
a `queued_notification` that is never processed would pin at its `sequence_num`, re-paging everything after it.

## Confirm live following on the deployed sync

The page's "last event" moves while a session runs and no page read follows the connect.
