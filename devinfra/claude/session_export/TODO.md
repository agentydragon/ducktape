# TODO

## Decide `events.payload`: `json` or `jsonb`

Ships as `jsonb` with a NUL rewrite ([docs/sync.md](docs/sync.md) § `payload`). The choice is open: check on a fuller
sample than the 3.2% measured (size, extraction timings, how the 151 NUL rows would be filtered out of `json`
queries) and decide whether byte-exact payloads outweigh containment queries and a clean scan.

Switching is one column type in `store.py` and `migrations/versions/0001_sessions.py`, and dropping `dumps_jsonb`'s
rewrite; deployed databases are disposable.
