# Running Props Evaluation with the OpenAI API

End-to-end procedure for running the props critic and grader against committed
specimen snapshots using the OpenAI Responses API (e.g., `gpt-5-mini`).

See <../local_llm_evaluation/evaluation.md> for the local LLM variant.

## Prerequisites

- A running props stack: PostgreSQL, OCI registry, backend with agent images
  pushed. The `/test_props setup` skill automates this.
- `OPENAI_API_KEY` environment variable set with a valid OpenAI key

## OpenAI-Specific Configuration

No `[upstreams.*]` or `[[models]]` sections are needed in the config file —
OpenAI models like `gpt-5-mini` are already in `model_metadata.yaml` with
`upstream_name=NULL`, which routes to the default OpenAI upstream automatically.

The backend needs `OPENAI_BASE_URL` set:

```bash
OPENAI_BASE_URL="https://api.openai.com/v1"
```

**Critical**: the URL must include `/v1`. The LLM proxy appends `/responses`
to the base URL — without `/v1`, the resulting URL
`https://api.openai.com/responses` returns 404.

## Running Critics

Start with file-set examples — they are faster and cheaper than whole-snapshot
runs. These examples have the most TP occurrences directly in scope (counted
via `critic_scopes_expected_to_recall` subset join):

| Rank | Snapshot                       | Files                                                        | `files_hash`                       | TPs | Occurrences |
| ---- | ------------------------------ | ------------------------------------------------------------ | ---------------------------------- | --- | ----------- |
| 1    | `ducktape/2025-09-03-00`       | `llm/.../git_commit_ai/cli.py`                               | `8e2209f20bd1df0c5bc4073dfff739fe` | 33  | 39          |
| 2    | `ducktape/2025-11-20-00`       | `adgn/.../persist/models.py`, `adgn/.../persist/sqlite.py`   | `bb8aff17944a6348a8089790457e3094` | 15  | 31          |
| 3    | `ducktape/2025-11-26-00`       | `adgn/.../git_commit_ai/cli.py`                              | `6e416fb1d095abc7fdc79131434c7dac` | 20  | 21          |
| 4    | `ducktape/2025-11-21-00`       | `adgn/.../mcp_bridge/servers/agents.py`                      | `15702f4d16234db852e973e31323fbdd` | 21  | 21          |
| 5    | `gmail-archiver/2025-12-17-00` | `gmail_archiver/cli/common.py`, `gmail_archiver/__main__.py` | `9e218584782810e5a65195da8f63931a` | 14  | 21          |

```bash
ADMIN_TOKEN="<from backend startup logs>"

# File-set example (fast, cheap)
curl -s -X POST 'http://localhost:8000/api/runs/critic' \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "definition_id": "latest",
    "example": {
      "kind": "file_set",
      "snapshot_slug": "ducktape/2025-09-03-00",
      "files_hash": "8e2209f20bd1df0c5bc4073dfff739fe"
    },
    "critic_model": "gpt-5-mini",
    "timeout_seconds": 1800,
    "budget_usd": 5.0
  }'

# Whole-snapshot run (264 TPs, slower)
curl -s -X POST 'http://localhost:8000/api/runs/critic' \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "definition_id": "latest",
    "example": {"kind": "whole_snapshot", "snapshot_slug": "ducktape/2025-12-04-00"},
    "critic_model": "gpt-5-mini",
    "timeout_seconds": 1800,
    "budget_usd": 5.0
  }'
```

The `GraderSupervisor` (enabled by `grader_model` in the config) automatically
grades each critic's output after it finishes. Monitor with:

```bash
psql -c "SELECT agent_run_id, type_config->>'agent_type' AS type, model, status,
         container_exit_code FROM agent_runs ORDER BY created_at"
```

Wait until both critic and grader runs show `status = 'exited'` with
`container_exit_code = 0`.

## Exporting Results

Export run results (excluding ground truth and infrastructure tables) for
another session to import:

```bash
pg_dump -Fc eval_results \
  --data-only --no-owner --no-privileges \
  --exclude-table=true_positives \
  --exclude-table=true_positive_occurrences \
  --exclude-table=false_positives \
  --exclude-table=false_positive_occurrences \
  --exclude-table=fp_occurrence_relevant_files \
  --exclude-table=occurrence_ranges \
  --exclude-table=critic_scopes_expected_to_recall \
  --exclude-table=file_sets \
  --exclude-table=file_set_members \
  --exclude-table=snapshots \
  --exclude-table=snapshot_files \
  --exclude-table=model_metadata \
  --exclude-table=agent_role_salt \
  --exclude-table=alembic_version \
  -f props/docs/openai_evaluation/results.dump
```

Uses custom archive format (`-Fc`) which is compressed and avoids the
`\restrict` psql meta-commands that plain-text dumps emit since CVE-2025-8714.

Exports: `agent_definitions`, `agent_runs`, `reported_issues`,
`reported_issue_occurrences`, `grading_edges`, `issue_clusters`,
`issue_cluster_members`, `llm_requests`.

## Importing Results

To continue from an exported dump in a new session:

1. Set up a running props stack (PostgreSQL + `db recreate` to sync ground truth)
2. Import:

```bash
pg_restore --disable-triggers -d eval_results \
  props/docs/openai_evaluation/results.dump
```

The circular FKs (`agent_runs` <-> `agent_definitions`) are `DEFERRABLE
INITIALLY DEFERRED`, so insert order doesn't matter. `--disable-triggers`
prevents RLS INSERT policy failures during restore.
