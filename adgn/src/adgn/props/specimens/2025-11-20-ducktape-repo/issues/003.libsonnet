local I = import '../../specimens/lib.libsonnet';

// iss-003: Missing typed stubs and fixture factories in approval_policy tests

I.issueOneOccurrence(
  rationale=|||
    The test file `test_policy_resources.py` is missing patterns used in other MCP tests:

    **1. Missing server stubs (similar to issue 001 for test_proposals_resources.py):**
    - Tests use raw `._mcp_server.call_tool()` and `._mcp_server.read_resource()` methods
    - Other MCP tests (exec, editor, chat) use typed stub classes for type safety
    - Pattern: Create stubs inheriting from ServerStub with typed methods (see exec_stubs.py)
    - Should create `adgn/src/adgn/mcp/testing/approval_policy_stubs.py` with:
      - `ApprovalPolicyServerStub` (reader)
      - `ApprovalPolicyProposerServerStub`
      - `ApprovalPolicyAdminServerStub`
    - Typed methods provide IDE completion and type checking for tool arguments/returns

    **2. Missing fixture factories:**
    - Server instances created in test class fixtures (lines 53-61)
    - Should use shared fixtures in conftest.py or fixture factories
    - Reduces duplication and makes server setup consistent across test files

    **3. Poor assertion style - multiple individual field assertions:**
    Lines 213-218, 249-252: Asserting each field individually instead of structured comparison:
    ```python
    policy = await persistence.get_policy("minimal")
    assert policy is not None
    assert policy.id == "minimal"
    assert policy.text == "pass"
    assert policy.description is None
    assert policy.enabled is True  # default
    ```

    Should use one of two patterns:
    a) Pydantic model equality (used in exec tests):
       `assert policy == Policy(id="minimal", text="pass", description=None, enabled=True)`
    b) Hamcrest has_properties (used in agent tests):
       `assert_that(policy, has_properties(id="minimal", text="pass", description=None, enabled=True))`

    Both provide:
    - Single assertion with clear expected structure
    - Better error messages showing full diff on failure
    - Less verbose code

    Similar patterns at lines 171-176, 308-309, 320-321.
  |||,
  properties=['test-quality', 'type-safety', 'maintainability', 'consistency'],
  filesToRanges={
    'adgn/tests/mcp/approval_policy/test_policy_resources.py': [
      [53, 61],   // Server fixtures - should share with conftest
      [70, 70],   // Raw ._mcp_server access - should use stub
      [82, 90],   // Raw call_tool - should use typed stub method
      [103, 103], // Raw read_resource - should use stub
      [130, 138], // Raw call_tool
      [141, 141], // Raw read_resource
      [158, 166], // Raw call_tool
      [171, 176], // Individual field assertions
      [181, 197], // Raw call_tool
      [203, 209], // Raw call_tool
      [213, 218], // Individual field assertions (user example)
      [227, 244], // Raw call_tool
      [249, 252], // Individual field assertions
      [256, 263], // Raw call_tool
      [270, 285], // Raw call_tool
      [289, 290], // Individual field assertion
      [300, 305], // Raw call_tool
      [308, 309], // Individual field assertion
      [312, 315], // Raw call_tool
      [320, 321], // Individual field assertion
      [325, 328], // Raw call_tool
      [366, 372], // Raw call_tool
      [381, 384], // Raw call_tool
    ],
  },
)
