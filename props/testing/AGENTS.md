# Props test data

The root `props/conftest.py` holds configuration and tracing only. Opt in to shared fixture
modules with `pytest_plugins` in each test module and a `gazelle:include_dep` for each plugin.
Register `testing.postgres_fixtures` before `props.testing.fixtures.db` so Props' synchronous
`engine` fixture overrides the shared asynchronous default. Agent container E2E tests register
`props.agents.testing.e2e_pytest_plugins`, which bundles these with `runs`, `scopes`, `e2e_infra`,
`e2e_container` and `mcp_infra.testing.fixtures` (`async_docker_client`).

Git fixtures in `testing/fixtures/testdata/specimens/test-fixtures/` are the single source of
truth for test data. Never construct `Snapshot`/`TruePositive`/`FalsePositive`/`Example` ORM
rows in tests: depend on `synced_db` and query them. Missing coverage → extend the git fixtures.

| Specimen | Split | Use for                                                    |
| -------- | ----- | ---------------------------------------------------------- |
| `train1` | TRAIN | multi-file, duplication detection, RLS train (6 TPs, 1 FP) |
| `valid1` | VALID | RLS valid split, warm-start validation                     |
| `valid2` | VALID | warm-start with multiple validation examples               |
| `test1`  | TEST  | RLS test split                                             |

Fixtures in `testing/fixtures/` (use them; never hardcode `tp-001`/`occ-001`/`fp-001` or build
scopes inline):

- `scopes.py`: `subtract_file_example`, `all_files_scope`
- `ground_truth.py`: `example_subtract_orm`, `example_multi_tp_orm`, `tp_occurrence_single`,
  `fp_id`, `fp_occurrence_id` (prefer these single-field ID fixtures; tuple fixtures are for
  iteration only)
- `runs.py`: `make_fake_{critic,grader}_run`, `make_fake_grader_run_with_credit`,
  `make_llm_request` (priced spend for budget tests), `test_snapshot`
- `e2e_container.py`: `e2e_stack` (full Docker stack)
- `db.py`: `synced_db`, `synced_test_session`
