## Critic Developer System Access

In addition to database access, you also connect to the **Backend HTTP API** at `PROPS_BACKEND_URL` for evaluation orchestration.

### Source Code Access

Critic developer agents can fetch snapshots on demand using the `fetch_snapshot` tool:

```bash
# Use the fetch_snapshot tool, or via Python:
python3 -c "from props.db.snapshot_io import fetch_snapshot_to_path; from props.db.database import Database; fetch_snapshot_to_path('ducktape/2025-11-26-00', Path('/workspace'), Database.from_env())"
```

### Database Access

| Table / View                       | `SELECT`              | `INSERT` | `UPDATE` |
| ---------------------------------- | --------------------- | -------- | -------- |
| `examples`                         | TRAIN split only [^1] | -        | -        |
| `true_positives`                   | TRAIN split           | -        | -        |
| `true_positive_occurrences`        | TRAIN split           | -        | -        |
| `false_positives`                  | TRAIN split           | -        | -        |
| `false_positive_occurrences`       | TRAIN split           | -        | -        |
| `critic_scopes_expected_to_recall` | TRAIN split           | -        | -        |
| `agent_runs`                       | TRAIN split children  | -        | -        |
| `agent_definitions`                | Own definitions       | -        | -        |
| `llm_requests`                     | TRAIN split children  | -        | -        |
| `recall_by_definition_split_kind`  | All splits (view)     | -        | -        |
| `recall_by_definition_example`     | All splits (view)     | -        | -        |
| `tp_occurrence_credits`            | All splits (view)     | -        | -        |

[^1]: VALID/TEST access restricted to prevent overfitting. See the Evaluation Flow section below for details.

**Note:** `agent_definitions` rows are created automatically when you push images to the registry proxy — no manual INSERT needed.

### Stats API

The backend exposes stats endpoints at `$PROPS_BACKEND_URL/api/stats/` that aggregate metrics across runs. These use your agent credentials (RLS-scoped). Read the full OpenAPI schema at `$PROPS_BACKEND_URL/openapi.json` for request/response types.

| Endpoint                          | Purpose                                                        |
| --------------------------------- | -------------------------------------------------------------- |
| `GET /api/stats/overview`         | Definitions leaderboard with recall, example counts by split   |
| `GET /api/stats/definitions/{id}` | Per-definition stats: split breakdown, per-example recall      |
| `GET /api/stats/examples`         | Per-example stats: which definitions perform best              |
| `GET /api/stats/occurrences`      | Per-TP-occurrence credit stats (mean/min/max/n_runs), sortable |
| `GET /api/stats/coverage`         | Coverage matrix + recall/TP count histograms for a split       |

Example:

```bash
curl -u "$PGUSER:$PGPASSWORD" "$PROPS_BACKEND_URL/api/stats/occurrences?split=train&sort_by=mean_credit&sort_dir=asc&limit=20"
```

These endpoints aggregate data from `tp_occurrence_credits` and `recall_by_definition_split_kind` views. You can also query these views directly via SQL if you need custom aggregations.

### Monitoring Grading Status

Monitor grading via the `grading_pending` view — it shows all `(critique_issue, ground_truth_occurrence)` pairs needing edges. Grading is complete when no rows remain for a given critique run.

Use the `wait_until_graded_tool` tool or `wait_until_graded()` from `props.agents.critic_dev.grading` for programmatic polling.
