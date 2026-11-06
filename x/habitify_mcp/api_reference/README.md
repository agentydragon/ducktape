# Habitify API reference captures

[`API_REFERENCE.md`](API_REFERENCE.md) summarizes observed endpoint behavior and links every retained YAML request/response capture. Captures are historical, not a live API contract; habit names and the `Authorization` value are masked.

## Collector warning

[`collect_references.py`](collect_references.py) is destructive and makes live writes. It deletes every `*.yaml` file in this directory, selects the first real habit returned by `GET /habits`, then sends four `PUT /status/{id}` requests for the fixed date `2025-05-09T00:00:00+00:00` (completed with a value, skipped, failed, then completed without a value). These requests can overwrite that habit's status, note, and value for that date. Run it only when replacing all captures and mutating that account are intended.

The client and collector send the API key directly in `Authorization` (no `Bearer` prefix); saved YAML masks it as `API_KEY_MASKED`. To inspect authentication behavior, see [`habitify_client.py`](../habitify_client.py) and the collector source.
