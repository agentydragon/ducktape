local I = import '../../specimens/lib.libsonnet';

// iss-052: Error-swallowing exception handler and duplicated responses_create fixture pattern

I.issueOneOccurrence(
  rationale=|||
    Two issues in test_mcp_concurrent.py (and across the test suite):

    **Issue 1: Error-swallowing exception handler (lines 75-81)**

    The test swallows all exceptions with a bare `except Exception:` block:

    ```python
    # Auto-approve all pending approvals by clicking approve repeatedly
    for _ in range(15):  # 5 agents x 3 calls each = 15 approvals
        try:
            approve_btn = page.get_by_role("button", name="Approve").first
            if approve_btn.count() > 0:
                approve_btn.click()
                page.wait_for_timeout(100)  # Small delay between approvals
        except Exception:
            break
    ```

    This hides actual errors that might occur during the approval process. If the button
    click fails for a real reason (element not found, page crashed, etc.), the test
    silently continues and may pass when it should fail.

    Fix: Remove the try/except entirely, or catch only specific expected exceptions
    (e.g., TimeoutError) and let real errors propagate. The loop should fail fast if
    something goes wrong.

    **Issue 2: Duplicated responses_create pattern (lines 102-110, 161-168, 271-282)**

    The pattern of creating stateful mock response handlers appears 16+ times across
    the test suite:

    ```python
    state = {"i": 0}

    async def responses_create(_req):
        i = state["i"]
        state["i"] = i + 1
        if i == 0:
            return responses_factory.make_tool_call(
                build_mcp_function("echo", "echo"), {"text": "first call"}, call_id="call_echo_1"
            )
        return responses_factory.make_tool_call(build_mcp_function("ui", "end_turn"), {}, call_id="call_ui_end")
    ```

    This appears in:
    - test_mcp_concurrent.py (3 instances: lines 102-110, 161-168, 271-282)
    - 13+ other test files (test_abort.py, test_approvals.py, test_mcp_edge_cases.py, etc.)

    Fix: Extract into a shared pytest fixture or helper function like:

    ```python
    # In tests/agent/helpers.py or conftest.py
    def make_stateful_responses(responses_factory, response_sequence):
        """Create a stateful mock response handler.

        Args:
            responses_factory: The responses factory fixture
            response_sequence: List of (function_name, server_name, params) tuples
                              or callable that takes call_index -> (fn, server, params)

        Returns:
            Callable suitable for use with make_mock()
        """
        state = {"i": 0}

        async def responses_create(_req):
            i = state["i"]
            state["i"] = i + 1

            if callable(response_sequence):
                fn_name, server_name, params = response_sequence(i)
            else:
                if i >= len(response_sequence):
                    # Default to end_turn
                    fn_name, server_name, params = ("end_turn", "ui", {})
                else:
                    fn_name, server_name, params = response_sequence[i]

            return responses_factory.make_tool_call(
                build_mcp_function(server_name, fn_name),
                params,
                call_id=f"call_{fn_name}_{i}"
            )

        return responses_create
    ```

    Usage:
    ```python
    # Instead of defining state + responses_create inline:
    responses = make_stateful_responses(responses_factory, [
        ("echo", "echo", {"text": "first call"}),
        ("end_turn", "ui", {}),
    ])
    s = run_server(lambda model: make_mock(responses))
    ```

    This would eliminate 40+ lines of duplicated code across the test suite.
  |||,
  properties=['python/no-swallowing-errors', 'no-oneoff-vars-and-trivial-wrappers'],
  filesToRanges={
    'adgn/tests/agent/e2e/test_mcp_concurrent.py': [
      [75, 82],   // Error-swallowing exception handler
      [100, 110], // Duplicated responses_create pattern #1
      [159, 169], // Duplicated responses_create pattern #2
      [269, 283], // Duplicated responses_create pattern #3
    ],
  },
  gap_note=|||
    The duplicated responses_create pattern suggests a property like "extract-test-fixtures":
    when the same test setup/mock pattern appears across multiple test files (especially
    stateful mocks or complex fixtures), it should be extracted into a shared fixture or
    helper function. This is distinct from general "no-oneoff-vars-and-trivial-wrappers"
    as it specifically addresses test code organization and the pytest fixture pattern.
  |||,
)
