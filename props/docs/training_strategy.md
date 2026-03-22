# Training Strategy: Implementation Notes

## Key Files

- `db/examples.py` - Example ORM model
- `db/sync/sync.py` - `generate_examples_for_snapshot()` — auto-generates examples from `critic_scopes_expected_to_recall` data
- `core/gepa/gepa_adapter.py` - GEPA integration (loads training examples from DB via ORM)

## Database Sync

Training examples are auto-generated during database sync:

```bash
props db sync
```

The YAML issue files (`.yaml`) define `critic_scopes_expected_to_recall` data which drives example generation.
