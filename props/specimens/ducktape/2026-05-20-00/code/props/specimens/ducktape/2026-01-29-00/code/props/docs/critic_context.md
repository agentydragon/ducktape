# What Critic Agents See

When a critic runs on an example, it receives:

1. **Snapshot slug and scope** — from its agent run row in the database (via `examples` table lookup)
2. **Source code** — mounted read-only at `/snapshots/{snapshot_slug}/`

The init script prints these in a "Review Scope" section:

```
=== Review Scope ===
Snapshot: ducktape/2025-11-26-00
Source code location: /snapshots/ducktape/2025-11-26-00
Files to review: src/server.py
```

## What Critics Do NOT See

- Ground truth (`true_positives`, `false_positives` tables)
- Other examples or their scopes
- Grader results or metrics
- Execution traces from other runs

## Scope Interpretation

The scope is displayed by the init script. Scope types:

- `ALL files in snapshot` → Review all files in the snapshot
- Comma-separated list → Review only those specific files

## Available Tools

Critics have access to:

- Shell tools via docker_exec (rg, ruff, mypy, vulture, etc.)
- Direct psql access with RLS-scoped credentials
- CLI `props critic-agent` for reporting issues
- MCP tool `critic_submit` to finalize review
