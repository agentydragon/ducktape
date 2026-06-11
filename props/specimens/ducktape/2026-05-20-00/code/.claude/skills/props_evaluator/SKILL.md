---
name: props_evaluator
description: Operate the live props cluster as evaluator — fetch credentials from k8s, call the API at props.allegedly.works, trigger critic/grader runs, and inspect results.
allowed-tools: Bash, Read, Grep, Glob, WebFetch, Task
---

# Props Evaluator Access

Operate the production props deployment at `https://props.allegedly.works` using
evaluator credentials retrieved from the cluster.

## Credentials

The evaluator password lives in a k8s secret scoped to the `props` namespace:

```bash
EVALUATOR_PASSWORD=$(kubectl get secret props-evaluator-credentials -n props \
  -o jsonpath='{.data.password}' | base64 -d)
EVALUATOR_CREDS=$(echo -n "evaluator:$EVALUATOR_PASSWORD" | base64 -w0)
```

Use as a `Bearer` token on every request:

```bash
curl -s -H "Authorization: Bearer $EVALUATOR_CREDS" \
  https://props.allegedly.works/api/stats/overview
```

## Base URL

`https://props.allegedly.works`

Health check: `GET /health` → `{"status":"ok"}`

## API Reference

@props/docs/backend_api.md

### Access by caller role

All `/api/gt/*` and `/api/stats/*` endpoints use per-caller RLS via the caller's
Postgres credentials — there is no agent-type gate at the HTTP layer.

| Caller                | `/api/gt/*`            | `/api/stats/*`         | `/api/runs/*` |
| --------------------- | ---------------------- | ---------------------- | ------------- |
| Admin (postgres user) | All data               | All data               | Full access   |
| Evaluator             | All data (BYPASSRLS)   | All data (BYPASSRLS)   | Read-only     |
| `critic_dev_optimize` | TRAIN split only       | TRAIN split only       | Read-only     |
| `critic`              | Own run only           | Own run only           | Read-only     |
| `grader`              | Assigned snapshot only | Assigned snapshot only | Read-only     |

Full endpoint list and request shapes: fetch `/openapi.json` from the live server.

## Triggering Runs

```bash
# POST /api/runs/critic — trigger a critic run
curl -s -X POST https://props.allegedly.works/api/runs/critic \
  -H "Authorization: Bearer $EVALUATOR_CREDS" \
  -H "Content-Type: application/json" \
  -d '{
    "definition_id": "latest",
    "example": {
      "kind": "file_set",
      "snapshot_slug": "ducktape/2025-09-03-00",
      "files_hash": "8e2209f20bd1df0c5bc4073dfff739fe"
    },
    "critic_model": "gpt-oss-20b-128k",
    "timeout_seconds": 1800,
    "budget_usd": 0.0
  }'
```

`budget_usd = 0.0` for `gpt-oss-20b-128k` (cluster inference is free).

Top file-set examples (fastest to run):

| Rank | `snapshot_slug`                | `files_hash`                       | TPs | Occurrences |
| ---- | ------------------------------ | ---------------------------------- | --- | ----------- |
| 1    | `ducktape/2025-09-03-00`       | `8e2209f20bd1df0c5bc4073dfff739fe` | 33  | 39          |
| 2    | `ducktape/2025-11-20-00`       | `bb8aff17944a6348a8089790457e3094` | 15  | 31          |
| 3    | `ducktape/2025-11-26-00`       | `6e416fb1d095abc7fdc79131434c7dac` | 20  | 21          |
| 4    | `ducktape/2025-11-21-00`       | `15702f4d16234db852e973e31323fbdd` | 21  | 21          |
| 5    | `gmail-archiver/2025-12-17-00` | `9e218584782810e5a65195da8f63931a` | 14  | 21          |

## Polling Run Status

```bash
# List recent runs
curl -s -H "Authorization: Bearer $EVALUATOR_CREDS" \
  "https://props.allegedly.works/api/runs?limit=10" | python3 -m json.tool

# Get specific run
curl -s -H "Authorization: Bearer $EVALUATOR_CREDS" \
  "https://props.allegedly.works/api/runs/<run_id>" | python3 -m json.tool

# Active runs
curl -s -H "Authorization: Bearer $EVALUATOR_CREDS" \
  https://props.allegedly.works/api/runs/active | python3 -m json.tool
```

## Viewing Stats

```bash
# Overview (definitions + example counts)
curl -s -H "Authorization: Bearer $EVALUATOR_CREDS" \
  https://props.allegedly.works/api/stats/overview | python3 -m json.tool

# Per-definition performance by image digest
curl -s -H "Authorization: Bearer $EVALUATOR_CREDS" \
  "https://props.allegedly.works/api/stats/definitions/<image_digest>" | python3 -m json.tool
```
