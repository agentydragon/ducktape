# Props Tests Analysis

Generated: 2025-12-23

## Failing Tests Diagnosis

### 1. `test_specimen_references_are_valid`

**Status**: Timing out during fixture setup (`synced_production_db`)

The test requires syncing ALL production specimens which takes too long.
This is a legitimate slow test that should have a higher timeout or be marked as integration-only.

---

## Test Fixture Types: Fast vs Slow

### `synced_db` (FAST - test fixtures)

Uses local git-tracked fixtures at `tests/props/fixtures/specimens/`:

- `test-fixtures/train1` (TRAIN) - 4 small Python files
- `test-fixtures/valid1` (VALID) - 1 file
- `test-fixtures/valid2` (VALID) - 1 file
- `test-fixtures/test1` (TEST) - 1 file

These use `vcs: local` with `bundle: null` in their `manifest.yaml`, so:

- **No hydration** - `resolve_source_root()` returns the original path directly with `needs_cleanup=False`
- **No network** - No GitHub/Git cloning
- **Fast sync** - Only reads a handful of small files for line counting

Most tests use this fixture and sync should complete in <1 second.

### `synced_production_db` (SLOW - real specimens)

Uses production specimens from `ADGN_PROPS_SPECIMENS_ROOT` (typically `~/code/specimens`):

- Multiple repositories with Git/GitHub sources
- **Hydration required** - Downloads/clones repos, extracts tarballs
- **Line counting** - Reads every file in every snapshot

This explains the 35s setup times for tests like `test_specimen_has_valid_split`.

Tests marked `@pytest.mark.requires_production_specimens` use this fixture.
To skip them: `pytest -m 'not requires_production_specimens'`

---

## Slow Tests Summary

Total test time: ~627s (~10.5 minutes) for 243 tests

- 5 failed
- 228 passed
- 10 skipped

## Slowest 30 Tests

| Duration | Phase | Test                                                                                           |
| -------- | ----- | ---------------------------------------------------------------------------------------------- |
| 72.30s   | call  | `critic_dev/optimize/test_e2e.py::test_optimizer_orchestrates_critic`                          |
| 35.19s   | setup | `db/test_splits.py::test_specimen_has_valid_split`                                             |
| 32.65s   | call  | `critic/test_e2e.py::test_critic_http_mode_submit_with_issues`                                 |
| 29.16s   | setup | `grader/test_e2e.py::test_grader_comprehensive_data_access`                                    |
| 19.54s   | call  | `critic_dev/optimize/test_e2e.py::test_cli_hard_examples_shows_metrics`                        |
| 19.31s   | call  | `critic/test_e2e.py::test_critic_http_mode_zero_issues`                                        |
| 19.31s   | call  | `critic_dev/optimize/test_e2e.py::test_cli_leaderboard_shows_recall`                           |
| 19.29s   | call  | `critic/test_e2e.py::test_critic_does_not_infinite_loop_on_zero_issues`                        |
| 19.10s   | call  | `critic_dev/optimize/test_e2e.py::test_optimizer_critic_workflow`                              |
| 18.04s   | call  | `critic/test_e2e.py::test_critic_zero_issues`                                                  |
| 17.52s   | setup | `grader/test_e2e.py::test_grader_http_mode_sql_workflow`                                       |
| 17.44s   | call  | `grader/test_e2e.py::test_grader_http_mode_sql_workflow`                                       |
| 17.40s   | setup | `grader/test_e2e.py::test_grader_http_mode_zero_issues`                                        |
| 16.75s   | call  | `specimens/test_validation.py::test_specimen_issues_and_false_positives_load`                  |
| 16.72s   | call  | `grader/test_e2e.py::test_grader_http_mode_zero_issues`                                        |
| 16.13s   | call  | `db/test_splits.py::test_all_specimens_in_splits_can_load`                                     |
| 14.67s   | call  | `grader/test_e2e.py::test_grader_comprehensive_data_access`                                    |
| 12.92s   | call  | `critic_dev/improve/test_e2e.py::test_cli_hard_examples_in_improvement_agent`                  |
| 12.79s   | call  | `critic_dev/improve/test_e2e.py::test_prompt_improve_e2e_multiple_examples`                    |
| 12.31s   | call  | `critic_dev/improve/test_e2e.py::test_prompt_improve_e2e_creates_package`                      |
| 12.29s   | call  | `critic_dev/improve/test_e2e.py::test_cli_leaderboard_in_improvement_agent`                    |
| 6.12s    | call  | `critic/test_temp_user_permissions.py::test_docker_minimal_insert`                             |
| 1.68s    | setup | `critic/test_critic_sql_integration.py::test_critic_sql_multi_location_occurrence`             |
| 1.63s    | setup | `critic/test_critic_sql_integration.py::test_critic_sql_rls_isolation`                         |
| 1.47s    | setup | `grader/test_grader_sql_integration.py::test_grader_report_failure_prevents_subsequent_submit` |
| 1.34s    | setup | `critic_dev/improve/test_e2e.py::test_prompt_improve_e2e_success`                              |
| 1.31s    | call  | `critic/test_temp_user_permissions.py::test_docker_connection_info`                            |
| 1.26s    | setup | `db/test_tp_occurrence_credits.py::test_occurrence_statistics_has_correct_n_critic_runs`       |

## Categories of Slow Tests

### 1. Docker Container Startup (~15-35s each)

Tests that spin up Docker containers for agent execution:

- All `*_http_mode_*` tests
- `test_docker_*` tests in temp_user_permissions

**Root cause**: Container creation, image pull checks, network setup, Python environment initialization inside container.

**Potential optimizations**:

- Reuse container across tests (session-scoped fixture)
- Pre-warm container pool
- Use lighter base image
- Keep container running between tests

### 2. Three-Agent Workflow (72s)

`test_optimizer_orchestrates_critic` is the slowest single test.

**Root cause**: Runs critic → grader → critic_dev_optimize sequentially, each with its own container lifecycle.

**Potential optimizations**:

- Mock LLM responses more aggressively
- Parallelize independent agent runs
- Share container between agents

### 3. Specimen Loading (16-35s) - Production Specimens Only

- `test_specimen_has_valid_split` (35s setup)
- `test_specimen_issues_and_false_positives_load` (17s)
- `test_all_specimens_in_splits_can_load` (16s)

**These tests use `synced_production_db`** (not `synced_db`), so they sync real specimens:

**Root cause**: The `synced_production_db` fixture runs `sync_all()` which for GitHub/Git sources:

1. Loads snapshots from YAML (fast)
2. **Hydrates each snapshot** - downloads from GitHub or clones Git repos (SLOW)
3. **Reads every file and counts lines** for `snapshot_files` table (SLOW for large repos)
4. Syncs issues from YAML (medium)
5. Syncs file sets (fast)
6. Syncs model metadata (fast)
7. Syncs agent definitions (fast)

**Note**: For test fixtures (`synced_db`), snapshots use `vcs: local` so hydration is a no-op
(returns original path with `needs_cleanup=False`). Only file line counting happens, which is fast
for the ~8 small test files.

**Potential optimizations for production sync**:

- **Skip file line counting for tests** - if tests don't need `snapshot_files.line_count`, skip this step
- **Pre-sync database dump** - create a database dump and restore instead of syncing from scratch
- **Parallel hydration** - hydrate snapshots concurrently (currently sequential)
- **Lazy hydration** - only hydrate snapshots actually used by tests
- **Hydration caching** - archives are cached in `~/.cache/adgn-llm/snapshots/` but still need extraction

### 4. Expensive Setup Fixtures

Several tests have 15-35s setup phases:

- `test_grader_comprehensive_data_access` (29s setup)
- `test_grader_http_mode_sql_workflow` (17s setup)
- `test_grader_http_mode_zero_issues` (17s setup)

**Root cause**: Complex fixture chains, DB seeding, Docker preparation.

**Potential optimizations**:

- Session-scoped fixtures for shared infrastructure
- Parallel fixture setup
- Fixture caching

## Recommendations

### Quick Wins

1. **Session-scoped Docker client**: Avoid recreating Docker client per test
2. **Cache specimen parsing**: Parse YAML once per session
3. **Reuse containers**: Keep agent containers warm between tests

### Medium-term

1. **Container pooling**: Pre-create N containers at session start
2. **Fixture optimization**: Audit fixture scope (function → class → module → session)
3. **Parallel test groups**: Group tests by resource needs, run in parallel

### Long-term

1. **Mock more aggressively**: Replace Docker calls with mocks for unit tests
2. **Separate integration tests**: Mark slow tests, run separately in CI
3. **Profile fixture chains**: Find redundant setup/teardown
