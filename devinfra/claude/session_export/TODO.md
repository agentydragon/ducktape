# TODO

## Decide `events.payload`: `json` or `jsonb`

Ships as `jsonb` with a NUL rewrite ([docs/sync.md](docs/sync.md) § `payload`). The choice is open: check on a fuller
sample than the 3.2% measured (size, extraction timings, how the 151 NUL rows would be filtered out of `json`
queries) and decide whether byte-exact payloads outweigh containment queries and a clean scan.

Switching is one column type in `store.py`, a new migration, and dropping `dumps_jsonb`'s rewrite.

## Confirm live following on the deployed sync

The page's "last event" moves while a session runs and no page read follows the connect, and the stamps of a client
event sent while the session is followed (`received_at`, then `processing_at`) reach its row.
