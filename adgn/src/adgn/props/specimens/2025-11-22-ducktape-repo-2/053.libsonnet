local I = import '../../specimens/lib.libsonnet';

// iss-053: Tests for unimplemented UI features with error-swallowing fallbacks

I.issueOneOccurrence(
  rationale=|||
    The test_mcp_errors.py file contains multiple issues:

    **Issue 1: Testing unimplemented UI features with error-swallowing fallback (lines 44-55)**

    ```python
    try:
        # Wait for either an error message or connection failure indicator
        error_indicator = page.locator(".error, .alert-error, [data-testid='error-message']").first
        error_indicator.wait_for(state="visible", timeout=5000)
        # Verify error text mentions the problem
        error_text = error_indicator.inner_text()
        assert_that(error_text, has_length(greater_than(0)), "Error message should not be empty")
    except Exception:
        # Alternative: check if WS connection shows as disconnected/failed
        ws_status = page.locator(".ws .dot")
        # Should not show "on" (connected) state
        ws_status.wait_for(timeout=5000)
    ```

    Problems:
    1. **Tests unimplemented features**: The backend no longer implements the error UI elements
       (.error, .alert-error, [data-testid='error-message']) that this test checks for
    2. **Swallows all errors**: Bare `except Exception:` hides actual test failures
    3. **Two massively different alternatives**: Tests should NOT have fallback logic that
       accepts completely different behaviors. Either error indicators should appear OR the
       WS connection should show disconnected - the test should pick ONE expected behavior
       and fail if it doesn't happen. Having both as acceptable alternatives makes the test
       meaningless - it will pass regardless of what actually happens.

    This same pattern appears at lines 288-298 in test_subscription_to_deleted_agent().

    **Issue 2: Duplicated responses_create pattern (lines 73-82, 127-135, 184-193, 249-256)**

    The same stateful mock response pattern appears 4 times in this file alone:

    ```python
    state = {"i": 0}

    async def responses_create(_req):
        i = state["i"]
        state["i"] = i + 1
        if i == 0:
            return responses_factory.make_tool_call(...)
        return responses_factory.make_tool_call(build_mcp_function("ui", "end_turn"), {}, call_id="call_ui_end")
    ```

    This is the same duplication issue as finding 052.

    **Issue 3: Overuse of contextlib.suppress (lines 94-96, 102-103, 147-148, 153-155, 230-232)**

    Multiple uses of `with suppress(Exception):` to hide errors:

    ```python
    with suppress(Exception):
        # Server attachment might fail; we're testing error handling
        requests.patch(base + f"/api/agents/{agent_id}/mcp", json={"attach": spec})
    ```

    If the test is meant to verify error handling, it should explicitly check for expected
    errors, not suppress all exceptions. Tests that suppress exceptions provide no signal
    when they fail - they just silently skip over problems.

    Fix:
    1. Remove tests for unimplemented UI features, or implement the features first
    2. Remove error-swallowing try/except blocks and `suppress()` calls
    3. Pick ONE expected behavior per test and assert it happens
    4. Extract duplicated responses_create into shared fixture (see finding 052)
  |||,
  properties=['python/no-swallowing-errors', 'no-oneoff-vars-and-trivial-wrappers', 'no-dead-code'],
  filesToRanges={
    'adgn/tests/agent/e2e/test_mcp_errors.py': [
      [44, 55],   // Error-swallowing fallback for unimplemented error UI
      [288, 298], // Same pattern in another test
      [73, 82],   // Duplicated responses_create pattern #1
      [127, 135], // Duplicated responses_create pattern #2
      [184, 193], // Duplicated responses_create pattern #3
      [249, 256], // Duplicated responses_create pattern #4
      [94, 96],   // suppress(Exception) hiding errors
      [102, 103], // suppress(Exception) hiding errors
      [147, 148], // suppress(Exception) hiding errors
      [153, 155], // suppress(Exception) hiding errors
      [230, 232], // suppress(Exception) hiding errors
    ],
  },
  gap_note=|||
    This deserves a property like "no-alternative-test-paths": tests should verify one specific
    expected behavior, not accept multiple completely different outcomes as valid. When a test
    says "either X should happen OR Y should happen", it's not really testing anything - it will
    pass regardless of what the system does. Tests should fail fast when expectations aren't met,
    not fall back to checking something completely different.

    Related to "no-dead-code" (tests for unimplemented features) and "python/no-swallowing-errors"
    (error suppression), but specifically about test design anti-patterns.
  |||,
)
