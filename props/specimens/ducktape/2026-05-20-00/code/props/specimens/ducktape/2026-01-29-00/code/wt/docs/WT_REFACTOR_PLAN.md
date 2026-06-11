# WT Refactor Plan

## Scope

- Focus areas: `wt` server + client reliability, GitHub PR cache/refresh, watchers, handlers, and view rendering.
- Out of scope: major feature work, protocol shape changes, persistence, or unrelated LLM tooling.

## Outstanding Work

- Completed
  - GitHub PR list construction now uses Pydantic field names (`head_ref_name`, `merged_at` ISO) — `src/wt/server/github_client.py`
  - `PRService` catches `GitHubUnavailableError`; uses `asyncio.get_running_loop` — `src/wt/server/pr_service.py`
  - GitHub refresh watcher stop is non-blocking via `asyncio.to_thread` — `src/wt/server/github_refresh.py`
  - Removed double initial refresh; single deterministic cache fill at startup — `src/wt/server/github_refresh.py` + `src/wt/server/pr_service.py`
  - Background task exception logging via `_log_task_done`; import hygiene tightened — `src/wt/server/handlers/status_handler.py`
  - Tests: removed dead PyGithub shadow code; robust PR fixtures via `WT_TEST_MODE` — `tests/wt/e2e/test_github_pr_display_real.py`
  - Repo-wide strict import cleanup (moved non-optional/logging imports to top); LLM notifications tests passing

1. **PR hyperlinks respect configured repo**
   File: `src/wt/client/view_formatter.py`
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
