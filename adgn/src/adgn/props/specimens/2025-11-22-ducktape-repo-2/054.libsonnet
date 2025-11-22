local I = import '../../specimens/lib.libsonnet';

// iss-054: Duplicated test fixtures and error-swallowing in test_mcp_edge_cases.py

I.issueOneOccurrence(
  rationale=|||
    The test_mcp_edge_cases.py file has similar issues to test_mcp_errors.py and test_mcp_concurrent.py:

    **Issue 1: Duplicated responses_create pattern (5 instances)**

    The stateful mock response pattern appears 5 times in this file:

    ```python
    state = {"i": 0}

    async def responses_create(_req):
        i = state["i"]
        state["i"] = i + 1
        if i == 0:
            return responses_factory.make_tool_call(...)
        return responses_factory.make_tool_call(build_mcp_function("ui", "end_turn"), {}, call_id="call_ui_end")
    ```

    Instances at:
    - Lines 38-51 (test_subscription_to_nonexistent_resource_uri)
    - Lines 100-101 (test_rapid_agent_create_delete, simpler version)
    - Lines 139-152 (test_mcp_server_disconnect_reconnect)
    - Lines 207-220 (test_network_interruption_during_resource_read)
    - Lines 275-283 (test_subscribe_before_mcp_connection_established)

    This is the same duplication documented in findings 052 and 053.

    **Issue 2: Error-swallowing exception handlers (lines 171-175, 251-255)**

    Multiple instances of bare `except Exception: pass` that hide test failures:

    ```python
    try:
        wait_for_pending_approvals(page, count=1, timeout=5000)
        approve_first_pending(page)
    except Exception:
        pass  # No approval needed
    ```

    If approvals are sometimes needed and sometimes not, the test should explicitly check
    which case it's in, not just swallow all exceptions. This hides real failures like
    page crashes, element not found errors, etc.

    Fix:
    1. Extract duplicated responses_create into shared fixture (see finding 052 for proposed implementation)
    2. Remove error-swallowing exception handlers
    3. Make test expectations explicit - either approval is needed or it isn't
  |||,
  properties=['no-oneoff-vars-and-trivial-wrappers', 'python/no-swallowing-errors'],
  filesToRanges={
    'adgn/tests/agent/e2e/test_mcp_edge_cases.py': [
      [38, 51],   // Duplicated responses_create pattern #1
      [100, 101], // Duplicated responses_create pattern #2 (simpler)
      [139, 152], // Duplicated responses_create pattern #3
      [207, 220], // Duplicated responses_create pattern #4
      [275, 283], // Duplicated responses_create pattern #5
      [171, 175], // Error-swallowing exception handler
      [251, 255], // Error-swallowing exception handler
    ],
  },
)
