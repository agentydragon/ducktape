# Training Strategy: Implementation Notes

## Key Files

- `db/examples.py` - Example ORM model
- `db/sync/sync.py` - `refresh_examples_matview()` — refreshes the `examples` materialized view built from `critic_scopes_expected_to_recall` data
- `agents/critic_dev/recipes/examples_and_scopes.py` - critic-dev access to training examples and scopes

## Database Sync

Training examples are the `examples` materialized view over synced issues;
`props db sync-specimen` refreshes it after syncing a specimen's bundle:

```bash
props db sync-specimen --code-tar <specimen_code.tar> --data-yaml <specimen_data.yaml>
```

The YAML issue files (`.yaml`) define `critic_scopes_expected_to_recall` data which drives example generation.
