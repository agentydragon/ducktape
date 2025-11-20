local I = import '../../specimens/lib.libsonnet';

// iss-004: Import placement, fixture verbosity, and assertion style issues

I.issueOneOccurrence(
  rationale=|||
    Three related code style issues across test files:

    **1. Imports inside fixture function (test_policy_resources.py lines 35-37):**
    ```python
    @pytest.fixture
    async def engine(persistence, docker_client: DockerClient):
        agent_id = "test-agent"
        # Create agent in persistence
        from fastmcp.mcp_config import MCPConfig
        from adgn.agent.persist import AgentMetadata
        ...
    ```

    Imports should be at module top level, not inside functions. This violates PEP 8
    and makes dependencies unclear. Move to top with other imports.

    **2. Verbose fixture with unnecessary variable and docstring (test_policy_resources.py lines 19-25):**
    ```python
    @pytest.fixture
    async def persistence(tmp_path):
        """Create a temporary SQLite persistence instance."""
        db_path = tmp_path / "test.db"
        persist = SQLitePersistence(db_path)
        await persist.ensure_schema()
        return persist
    ```

    Should be:
    ```python
    @pytest.fixture
    async def persistence(tmp_path):
        persist = SQLitePersistence(tmp_path / "test.db")
        await persist.ensure_schema()
        return persist
    ```
    - Inline `db_path` (single use, no benefit from variable)
    - Drop docstring (function name and code are self-documenting)

    **3. Multiple assertions that should use hamcrest all_of (test_runtime_timeout.py lines 38-40):**
    ```python
    assert_that(res_ok.exit, instance_of(Exited))
    assert res_ok.exit.exit_code == 0
    assert (res_ok.stdout or "") == "ok"
    ```

    Should be:
    ```python
    assert_that(res_ok, all_of(
        has_properties(exit=all_of(instance_of(Exited), has_properties(exit_code=0))),
        has_properties(stdout="ok")
    ))
    ```

    Or simpler:
    ```python
    assert_that(res_ok, has_properties(
        exit=all_of(instance_of(Exited), has_properties(exit_code=0)),
        stdout="ok"
    ))
    ```

    Issues:
    - Three separate assertions instead of one combined check
    - `or ""` is redundant (stdout is already a string type)
    - Hamcrest provides better error messages showing which property failed
  |||,
  properties=['code-style', 'test-quality', 'pep8', 'readability'],
  filesToRanges={
    'adgn/tests/mcp/approval_policy/test_policy_resources.py': [
      [35, 37],  // Imports inside fixture function
      [19, 25],  // Verbose fixture with unnecessary variable/docstring
    ],
    'adgn/tests/agent/test_runtime_timeout.py': [
      [38, 40],  // Multiple assertions instead of combined hamcrest matcher
    ],
  },
)
