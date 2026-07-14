# WT Refactor Plan

Server + client reliability, GitHub PR cache/refresh, watchers, handlers, and
view rendering.

## Scope

- Focus areas: `wt` server + client reliability, GitHub PR cache/refresh, watchers, handlers, and view rendering.
- Out of scope: major feature work, protocol shape changes, persistence, or unrelated LLM tooling.

## Outstanding Work

### P1

1. **PR hyperlinks respect configured repo**
   File: `wt/client/view_formatter.py`
   Action: When `config.github_repo` is set, emit `https://github.com/{owner_repo}/pull/{n}`; fall back to `http://go/pull/{n}` otherwise.
   Acceptance: Integration tests confirm clickable GitHub links; fallback remains when repo unset.

### P2 — Test Coverage

2. **Unit test for `GitHubInterface.pr_list`**
   Action: Add test asserting field names and `merged_at` serialization.
   Acceptance: Test passes and fails appropriately on regressions.

3. **Resilience test for `GitHubUnavailableError`**
   Action: Simulate `GitHubUnavailableError`; ensure cache stores error state without task crash.
   Acceptance: Test passes and fails appropriately on regressions.

### P3 — Tooling Guardrails (optional)

4. **Semgrep rules** (repo-level, low priority):
   - `pydantic-v2-alias-constructor`: forbid alias kwargs (`headRefName`, `mergedAt`) in constructors.
   - `asyncio-get_running_loop-in-async`: flag `get_event_loop()` inside async defs.
   - `broad-except-non-boundary`: flag broad `except` outside boundary sections with `logger.exception`.
     Acceptance: Rules land via housekeeping PR; repo lint passes with zero new violations.
