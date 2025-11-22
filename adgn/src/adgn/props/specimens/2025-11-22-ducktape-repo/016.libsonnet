{
  title: 'ApprovalPolicyAdminServerStub should be a shared pytest fixture',
  severity: 'minor',
  category: 'test-quality',
  locations: [
    {
      path: 'adgn/tests/agent/test_policy_validation_reload.py',
      lines: [47, 48, 57, 58, 67, 68, 84, 85, 97, 98, 109, 110, 131, 132],
      context: 'Inline stub creation in test functions',
    },
  ],
  description: |||
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
  |||,
  recommendation: |||
    Create a pytest fixture that returns a context manager or async context manager:

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
}
