# Testing Guide for Props Tests

## Core Principle

**Git fixtures are the single source of truth for ALL test data.**

Never create synthetic ORM models (Snapshot, TruePositive, FalsePositive, Example) directly in
tests. Use the git-tracked test fixtures in `testing/fixtures/testdata/specimens/` and the `synced_test_db`
pytest fixture.

## Available Git Fixtures

Located in `testing/fixtures/testdata/specimens/`:

- **test-fixtures/train1** (TRAIN split)
  - Files: add.py, subtract.py, multiply.py, divide.py
  - Issues: 5 TPs (tp-001 through tp-005), 1 FP (fp-001)
  - Use for: Multi-file tests, duplication detection, RLS train split

- **test-fixtures/valid1** (VALID split)
  - Files: sample_subtract.py
  - Issues: 1 TP (tp-001)
  - Use for: RLS valid split, warm-start validation

- **test-fixtures/valid2** (VALID split)
  - Files: calculator.py
  - Issues: 1 TP (tp-001)
  - Use for: Warm-start with multiple validation examples

- **test-fixtures/test1** (TEST split)
  - Files: example_module.py
  - Issues: 1 TP (tp-001)
  - Use for: RLS test split verification

## Using Git Fixtures in Tests (short form)

- Always depend on `synced_test_db` to seed the DB from git fixtures.
- Query examples/TPs/FPs from the DB, never fabricate IDs.
- Use shared factories from `props.core.testing.fixtures` (`make_critic_run`, `make_grader_run`, etc.).
- Scope fixtures: `add_py_scope`, `subtract_file_scope`, `multiply_py_scope`, `divide_py_scope`, `all_files_scope`.

## High-value fixtures (testing/fixtures/)

- **Scopes** (scopes.py): `subtract_file_scope`, `add_py_scope`, `multiply_py_scope`, `divide_py_scope`,
  `example_module_py_scope`, `calculator_py_scope`, `all_files_scope`.
- **Ground truth** (ground_truth.py): `example_subtract_orm` (1 TP occurrence), `example_multi_tp_orm` (multi-TP), `test_snapshot`, `test_validation_snapshot`, `tp_occurrence_single`, `fp_id`, `fp_occurrence_id`.
- **Runs** (runs.py): `make_critic_run`, `make_grader_run`, `make_grader_run_with_credit`.
- **E2E** (e2e.py): `test_registry` for Docker-based integration tests.
- **Database** (db.py): `synced_test_db`, `synced_test_session` for DB fixtures.

## Anti-patterns (do not)

- Fabricate snapshots/examples/TPs/FPs in tests—query from the synced DB instead.
- Hardcode IDs like `tp-001`, `occ-001`, `fp-001`; use the fixtures above.
- Build scopes inline; reuse scope fixtures to avoid drift.

## Fixture guidance

- If coverage is missing, extend the git fixtures rather than fabricating data in tests.
- Prefer the single-field ID fixtures for clarity; tuple fixtures stay for iteration only.
