local I = import '../../specimens/lib.libsonnet';

// iss-006: Duplicated SQLite persistence fixtures across agent tests

I.issueOneOccurrence(
  rationale=|||
    Multiple test files in adgn/tests/agent create their own SQLite persistence instances
    instead of using a shared fixture. This leads to duplication and inconsistency.

    **Current situation:**

    1. **test_policy_validation_reload.py (lines 18-36):**
       - Creates `engine_and_persistence` fixture that returns a tuple `(engine, persistence)`
       - Every test must destructure: `engine, _ = engine_and_persistence` or
         `engine, persistence = engine_and_persistence` (lines 41, 54, 68, 84, 105, 120, 131)
       - This is verbose and error-prone

    2. **mcp_bridge/test_separated_servers.py (lines 31-38):**
       - Creates persistence inside `infrastructure_registry` fixture
       - Pattern: `persistence = SQLitePersistence(temp_db); await persistence.ensure_schema()`

    3. **mcp_bridge/test_ui_auth.py (lines 20-27):**
       - Identical pattern to test_separated_servers.py
       - Creates persistence inside `infrastructure_registry` fixture

    **Existing good pattern:**
    - `adgn/tests/agent/persist/conftest.py` (lines 19-29) has a clean `persistence` fixture
    - Pattern: `SQLitePersistence(tmp_path / "test.db")` + `ensure_schema()`
    - This should be promoted to `adgn/tests/agent/conftest.py` for reuse

    **Recommended fixes:**

    1. **Create shared persistence fixture in adgn/tests/agent/conftest.py:**
       ```python
       @pytest.fixture
       async def persistence(tmp_path: Path) -> SQLitePersistence:
           persist = SQLitePersistence(tmp_path / "test.db")
           await persist.ensure_schema()
           return persist
       ```

    2. **Split test_policy_validation_reload.py fixtures to avoid destructuring:**
       Instead of `engine_and_persistence` returning tuple, create two fixtures:
       - `persistence` (or use shared one from conftest)
       - `engine(persistence, docker_client)` that depends on persistence

       Benefits:
       - Tests can just use `engine` fixture without destructuring
       - Tests that need both can request both fixtures separately
       - No more `engine, _ = engine_and_persistence` patterns

    3. **Update mcp_bridge tests to use shared persistence fixture:**
       - test_separated_servers.py and test_ui_auth.py should use shared fixture
       - Their `infrastructure_registry` fixtures should accept `persistence` parameter
       - Eliminates duplicate `SQLitePersistence(temp_db); ensure_schema()` code

    **Impact:**
    - Reduces duplication of persistence setup
    - Makes tests more maintainable
    - Consistent pattern across all agent tests
    - Eliminates verbose tuple destructuring
  |||,
  properties=['test-quality', 'dry-principle', 'fixture-design', 'maintainability'],
  filesToRanges={
    'adgn/tests/agent/test_policy_validation_reload.py': [
      [18, 36],  // engine_and_persistence fixture returning tuple
      [41, 41],  // engine, _ = destructuring
      [54, 54],  // engine, _ = destructuring
      [68, 68],  // engine, _ = destructuring
      [84, 84],  // engine, persistence = destructuring
      [105, 105], // engine, _ = destructuring
      [120, 120], // engine, _ = destructuring
      [131, 131], // engine, persistence = destructuring
    ],
    'adgn/tests/agent/mcp_bridge/test_separated_servers.py': [
      [31, 38],  // Duplicate persistence creation in infrastructure_registry
    ],
    'adgn/tests/agent/mcp_bridge/test_ui_auth.py': [
      [20, 27],  // Duplicate persistence creation in infrastructure_registry
    ],
  },
)
