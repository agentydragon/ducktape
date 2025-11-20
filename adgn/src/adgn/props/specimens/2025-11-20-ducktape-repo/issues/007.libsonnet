local I = import '../../specimens/lib.libsonnet';

// iss-007: Multiple test quality issues in test_policy_validation_reload.py

I.issueOneOccurrence(
  rationale=|||
    Several test quality issues that should be fixed:

    **1. Multiple assertions that should use hamcrest contains matcher (lines 62-63, 77-79):**
    ```python
    assert_that(result.errors, has_length(greater_than(0)))
    assert "Runtime validation failed" in result.errors[0]
    ```

    Should be single check:
    ```python
    assert_that(result.errors, has_item(contains_string("Runtime validation failed")))
    ```

    Benefits:
    - Single assertion instead of two
    - Better error message if fails (shows all errors)
    - More explicit about intent ("contains item with substring")

    **2. Unnecessary comment and variable (lines 86-88):**
    ```python
    # Save a policy to persistence
    new_policy = "print('from persistence')"
    await persistence.set_policy(engine.agent_id, content=new_policy)
    ```

    Should inline:
    ```python
    await persistence.set_policy(engine.agent_id, content="print('from persistence')")
    ```
    - Single use variable adds no value
    - Comment is redundant (code is self-documenting)

    **3. admin_server should be shared fixture (lines 43, 56, 70, 90, 107, 122, 133, 146):**
    Every test creates its own `ApprovalPolicyAdminServer(engine=engine)`. Should be a fixture
    that depends on `engine` fixture. Benefits:
    - DRY principle
    - Consistent setup
    - Easy to modify server configuration

    **4. Raw _mcp_server._tools access should use server stub (lines 46, 59, 73-75, 96, 111, 126, 149):**
    ```python
    await admin_server._mcp_server._tools["validate_policy"].fn(ValidatePolicyArgs(...))
    await admin_server._mcp_server._tools["reload_policy"].fn(ReloadPolicyArgs(...))
    ```

    Should use typed server stub (like exec_stubs.py pattern):
    ```python
    stub = ApprovalPolicyAdminServerStub.from_server(admin_server, session)
    await stub.validate_policy(ValidatePolicyArgs(...))
    await stub.reload_policy(ReloadPolicyArgs(...))
    ```

    Or use client fixture/fixture factory. Benefits:
    - Type safety
    - IDE completion
    - No accessing private _mcp_server internals
    - More ergonomic API

    **5. Test has __main__ block (lines 153-154):**
    ```python
    if __name__ == "__main__":
        pytest.main([__file__, "-v"])
    ```

    Pytest tests generally shouldn't have __main__ blocks. Run with `pytest` command instead.
    If needed for IDE convenience, use pytest's built-in mechanisms, but this is outdated pattern.
  |||,
  properties=['test-quality', 'hamcrest-matchers', 'fixture-design', 'api-ergonomics', 'dry-principle'],
  filesToRanges={
    'adgn/tests/agent/test_policy_validation_reload.py': [
      [62, 63],   // Multiple assertions for error checking
      [77, 79],   // Multiple assertions for error checking
      [86, 88],   // Unnecessary comment and single-use variable
      [43, 43],   // admin_server creation (test 1)
      [56, 56],   // admin_server creation (test 2)
      [70, 70],   // admin_server creation (test 3)
      [90, 90],   // admin_server creation (test 4)
      [107, 107], // admin_server creation (test 5)
      [122, 122], // admin_server creation (test 6)
      [133, 133], // admin_server creation (test 7)
      [146, 146], // admin_server creation (test 8)
      [46, 46],   // Raw _mcp_server._tools access
      [59, 59],   // Raw _mcp_server._tools access
      [73, 75],   // Raw _mcp_server._tools access
      [96, 96],   // Raw _mcp_server._tools access
      [111, 111], // Raw _mcp_server._tools access
      [126, 126], // Raw _mcp_server._tools access
      [149, 149], // Raw _mcp_server._tools access
      [153, 154], // __main__ block (should be removed)
    ],
  },
)
