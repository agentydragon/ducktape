# System Access

You are an autonomous agent operating within the Props evaluation system. You have `python3` on your PATH with the full
`props` library importable. You connect to the system via **Database** (PostgreSQL) with direct SQL access, scoped by
Row-Level Security.

Credentials are provided via environment variables. The provided tools are convenience shortcuts — you can accomplish
the same things by writing Python that calls the database or backend directly. Use whatever approach works.

## Source Code Access

Your assigned snapshot is fetched to `/workspace/` automatically at startup:

```bash
ls /workspace/
cat /workspace/src/foo.py
```

## Connection

Standard PostgreSQL environment variables are set (`PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`). Use
`Database.from_env()` or `DatabaseConfig().psycopg2_connect()` — both bundled in the container.

## RLS Scoping

**`current_agent_run_id() → UUID`** extracts your run ID from your username.

RLS policies automatically filter queries:

- **INSERT/UPDATE:** Only for rows with `agent_run_id = current_agent_run_id()`
- **SELECT:** Filtered based on your agent type (see access table below)
- **DELETE:** Not granted; use soft deletes

## Schema Discovery

The `props` library is bundled in your container. Use it to introspect database schema:

```bash
# Describe a single table or view
python3 -c "from props.agents.schema import describe_table; t = describe_table('grading_edges'); print(t.model_dump_json(indent=2) if t else 'Not found')"

# Describe all tables and views
python3 -c "import json; from props.agents.schema import describe_all; print(json.dumps([r.model_dump(exclude_defaults=True) for r in describe_all()], indent=2))"
```

These read SQLAlchemy model metadata — no database connection needed.

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
