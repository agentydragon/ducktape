# Database and File System Access

Your container has direct PostgreSQL access via environment variables, scoped by Row-Level Security.

## Source Code Access

Init scripts typically fetch snapshots using `props snapshot fetch <slug>`, placing them at `/snapshots/{slug}/`.

Example:

```bash
ls /snapshots/ducktape/2025-11-26-00/    # List files in a snapshot
cat /snapshots/test-fixtures/train1/add.py   # Read a file
```

Check your init output for which snapshots were fetched and their paths.

## Connection

Standard PostgreSQL environment variables are set:

- `PGHOST`, `PGPORT`, `PGDATABASE` — Connection details
- `PGUSER` — Your temporary username (pattern: `agent_{run_id}`)
- `PGPASSWORD` — Your temporary password

Connect with psql (uses `PG*` environment variables automatically):

```bash
psql -c "SELECT current_agent_run_id()"
```

Python:

```python
import os, psycopg2
conn = psycopg2.connect(
    host=os.environ["PGHOST"], port=os.environ["PGPORT"],
    dbname=os.environ["PGDATABASE"], user=os.environ["PGUSER"],
    password=os.environ["PGPASSWORD"],
)
```

## RLS Scoping

**`current_agent_run_id() → UUID`** extracts your run ID from your username.

RLS policies automatically filter queries:

- **INSERT/UPDATE:** Only for rows with `agent_run_id = current_agent_run_id()`
- **SELECT:** Filtered based on your agent type (see access table below)
- **DELETE:** Not granted; use soft deletes

**View actual policies:** Run `\d+ table_name` to see the RLS policies for any table. Policies are the authoritative source for access rules.

## Schema Discovery

```bash
psql -c "\dt"                          # List tables
psql -c "\d+ reported_issues"          # Describe table (columns, types, constraints)
psql -c "\dv"                          # List views
psql -c "\df current_*"                # List functions
```

Run `\d+ table_name` before writing queries to understand the schema.

## Agent-Specific Access

### Critic

| Table                        | `SELECT`        | `INSERT` | `UPDATE` |
| ---------------------------- | --------------- | -------- | -------- |
| `reported_issues`            | Own run         | Own run  | Own run  |
| `reported_issue_occurrences` | Own run         | Own run  | Own run  |
| `examples`                   | Current example | -        | -        |
| `true_positives`             | -               | -        | -        |
| `false_positives`            | -               | -        | -        |

**Note:** Critics have NO access to ground truth (TPs/FPs) — blind review.

### Grader

| Table                              | `SELECT`          | `INSERT` | `UPDATE` |
| ---------------------------------- | ----------------- | -------- | -------- |
| `grading_edges`                    | Own run           | Own run  | Own run  |
| `reported_issues`                  | Graded critic run | -        | -        |
| `reported_issue_occurrences`       | Graded critic run | -        | -        |
| `true_positives`                   | Graded snapshot   | -        | -        |
| `true_positive_occurrences`        | Graded snapshot   | -        | -        |
| `false_positives`                  | Graded snapshot   | -        | -        |
| `false_positive_occurrences`       | Graded snapshot   | -        | -        |
| `critic_scopes_expected_to_recall` | Graded snapshot   | -        | -        |

**Note:** Graders see ground truth for the snapshot being graded only.

### Prompt Optimizer / Improvement

| Table                              | `SELECT`              | `INSERT` | `UPDATE` |
| ---------------------------------- | --------------------- | -------- | -------- |
| `examples`                         | TRAIN split only [^1] | -        | -        |
| `true_positives`                   | TRAIN split           | -        | -        |
| `true_positive_occurrences`        | TRAIN split           | -        | -        |
| `false_positives`                  | TRAIN split           | -        | -        |
| `false_positive_occurrences`       | TRAIN split           | -        | -        |
| `critic_scopes_expected_to_recall` | TRAIN split           | -        | -        |
| `critic_runs`                      | TRAIN split           | -        | -        |
| `grader_runs`                      | TRAIN split           | -        | -        |
| `recall_by_definition_split_kind`  | All splits (view)     | -        | -        |

[^1]: VALID/TEST access restricted to prevent overfitting. See `db/evaluation_flow.md.j2` for details.
