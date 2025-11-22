local I = import '../../specimens/lib.libsonnet';

// iss-016: ApprovalPolicyAdminServerStub should be a shared pytest fixture

I.issueOneOccurrence(
  rationale=|||
    Every test function in `test_policy_validation_reload.py` creates an
    `ApprovalPolicyAdminServerStub` instance inline using the same pattern:

    ```python
    async with Client(admin_server) as session:
        stub = ApprovalPolicyAdminServerStub.from_server(admin_server, session)
        result = await stub.validate_policy(...)
    ```

    This appears in 7 different test functions (lines 47, 57, 67, 84, 97, 109, 131).

    This violates DRY (Don't Repeat Yourself) and makes tests:
    - **Harder to maintain**: Changes to stub initialization require updates in 7 places
    - **More verbose**: Each test has 2-3 extra lines of setup boilerplate
    - **Less focused**: Test intent is obscured by setup code

    Fix - create a pytest fixture that returns a context manager or async context manager:

    ```python
    from contextlib import asynccontextmanager
    from collections.abc import AsyncIterator

    @pytest.fixture
    @asynccontextmanager
    async def policy_admin_stub(
        admin_server: ApprovalPolicyAdminServer
    ) -> AsyncIterator[ApprovalPolicyAdminServerStub]:
        """Provide a connected stub for the admin server."""
        async with Client(admin_server) as session:
            yield ApprovalPolicyAdminServerStub.from_server(admin_server, session)
    ```

    Then simplify tests to:

    ```python
    async def test_validate_policy_valid(policy_admin_stub):
        """Test validating a valid policy."""
        result = await policy_admin_stub.validate_policy(
            ValidatePolicyArgs(source="print('hello')")
        )
        assert result.valid is True
        assert_that(result.errors, has_length(0))
    ```

    This eliminates 14+ lines of duplicate code across the test file.

    **Alternative**: If the fixture-as-context-manager pattern is too complex,
    use function-scoped fixture with explicit cleanup:

    ```python
    @pytest.fixture
    async def policy_admin_stub(admin_server):
        client = Client(admin_server)
        session = await client.__aenter__()
        stub = ApprovalPolicyAdminServerStub.from_server(admin_server, session)
        yield stub
        await client.__aexit__(None, None, None)
    ```
  |||,
  properties=['no-oneoff-vars-and-trivial-wrappers'],
  filesToRanges={
    'adgn/tests/agent/test_policy_validation_reload.py': [
      [47, 48],   // Test 1 stub creation
      [57, 58],   // Test 2 stub creation
      [67, 68],   // Test 3 stub creation
      [84, 85],   // Test 4 stub creation
      [97, 98],   // Test 5 stub creation
      [109, 110], // Test 6 stub creation
      [131, 132], // Test 7 stub creation
    ],
  },
  gap_note=|||
    This pattern deserves a property like "extract-test-fixtures": when test setup
    code is duplicated across multiple test functions, it should be extracted into
    pytest fixtures. This is more specific than general "no-oneoff-vars-and-trivial-wrappers"
    as it addresses test organization, DRY principle in test suites, and pytest
    best practices for sharing test setup/teardown logic.
  |||,
)
