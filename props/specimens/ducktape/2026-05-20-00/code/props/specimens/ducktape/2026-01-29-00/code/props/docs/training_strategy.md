# Training Strategy: Implementation Notes

This document covers implementation details for the training pipeline. For conceptual documentation, see `agent_defs/common/docs/db/`.

## Implementation Files

- `models/training_example.py` - TrainingExample model and TP filtering logic
- `db/models.py` - ORM models (Snapshot, TruePositive, FalsePositive, CriticRun, GraderRun, etc.)
- `db/examples.py` - Example ORM model (separate file due to import order)
- `db/sync/_sync.py` - `generate_examples_for_snapshot()` - auto-generates examples from `critic_scopes_expected_to_recall` data
- `db/datapoints.py` - `get_examples_for_split()` - loads examples for GEPA/training
- `gepa/gepa_adapter.py` - GEPA integration (loads training examples from database via ORM)

## Database Sync

Training examples are auto-generated during database sync:

```bash
props db sync
```

The YAML issue files (`.yaml`) define `critic_scopes_expected_to_recall` data which drives example generation.
