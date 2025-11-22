{
  title: 'Test functions should use pytest parameterization instead of loops or multiple subtests',
  severity: 'minor',
  category: 'test-quality',
  locations: [
    {
      path: 'adgn/tests/agent/persist/test_integration.py',
      lines: [288, 351],
      context: 'test_decision_outcome_variants uses for loop over test cases',
    },
    {
      path: 'adgn/tests/agent/persist/test_integration.py',
      lines: [355, 476],
      context: 'test_complex_calltoolresult_content_types contains 5 subtests in one function',
    },
    {
      path: 'adgn/tests/agent/persist/test_integration.py',
      lines: [533, 640],
      context: 'test_error_handling_and_data_validation contains 4+ subtests in one function',
    },
  ],
  description: |||
    Three test functions violate pytest best practices by either using loops to test
    multiple cases or bundling multiple independent tests into a single function:

    **1. test_decision_outcome_variants (lines 288-351)**
    Uses a for loop to iterate over 6 test cases:
    ```python
    outcomes_to_test = [
        (ApprovalOutcome.POLICY_ALLOW, True, "Policy auto-approved and executed"),
        (ApprovalOutcome.POLICY_DENY_ABORT, False, "Policy denied, aborted"),
        # ... 4 more cases
    ]

    for i, (outcome, should_execute, reason) in enumerate(outcomes_to_test):
        # Test logic for each outcome
        # ...
    ```

    **2. test_complex_calltoolresult_content_types (lines 355-476)**
    Contains 5 independent subtests in one function (marked with comments):
    - Test 1: Text content
    - Test 2: Image content
    - Test 3: Error content
    - Test 4: Mixed content (text + image)
    - Test 5: Empty content (edge case)

    Each subtest creates a record, saves it, retrieves it, and asserts independently.

    **3. test_error_handling_and_data_validation (lines 533-640+)**
    Contains 4+ independent subtests in one function (marked with comments):
    - Test 1: Manually corrupt the JSON in the database
    - Test 2: Insert record with missing required field in JSON
    - Test 3: Get non-existent call_id (should return None, not raise)
    - Test 4: Test with malformed timestamp

    Each subtest manipulates the database independently and asserts different error conditions.

    **Problems with this approach:**
    1. **Poor failure reporting**: If test 3 fails, tests 4-5 never run
    2. **Unclear test names**: Can't see which specific case failed without reading output
    3. **Hard to run individually**: Can't run just "test image content" or "test POLICY_ALLOW"
    4. **Violates pytest conventions**: One test function should test one thing
  |||,
  recommendation: |||
    **For test_decision_outcome_variants**: Use `@pytest.mark.parametrize`:

    ```python
    @pytest.mark.parametrize("outcome,should_execute,reason", [
        (ApprovalOutcome.POLICY_ALLOW, True, "Policy auto-approved and executed"),
        (ApprovalOutcome.POLICY_DENY_ABORT, False, "Policy denied, aborted"),
        (ApprovalOutcome.USER_APPROVE, True, "User approved, executed"),
        (ApprovalOutcome.USER_DENY_ABORT, False, "User denied, aborted"),
        (ApprovalOutcome.USER_DENY_CONTINUE, False, "User denied but continued"),
        (ApprovalOutcome.POLICY_DENY_CONTINUE, False, "Policy denied but continued"),
    ])
    async def test_decision_outcome_variants(
        persistence: SQLitePersistence,
        test_agent: str,
        outcome: ApprovalOutcome,
        should_execute: bool,
        reason: str
    ) -> None:
        """Test decision outcome type and execution pattern."""
        # Test logic for single outcome
        # ...
    ```

    **For test_complex_calltoolresult_content_types**: Split into 5 separate test functions:

    ```python
    async def test_calltoolresult_text_content(persistence, test_agent):
        """Test persistence of text content."""
        # Test 1 logic

    async def test_calltoolresult_image_content(persistence, test_agent):
        """Test persistence of image content."""
        # Test 2 logic

    async def test_calltoolresult_error_content(persistence, test_agent):
        """Test persistence of error content."""
        # Test 3 logic

    async def test_calltoolresult_mixed_content(persistence, test_agent):
        """Test persistence of mixed content types."""
        # Test 4 logic

    async def test_calltoolresult_empty_content(persistence, test_agent):
        """Test persistence of empty content (edge case)."""
        # Test 5 logic
    ```

    **For test_error_handling_and_data_validation**: Split into 4+ separate test functions:

    ```python
    async def test_error_on_corrupted_json(tmp_path, test_agent):
        """Test error handling when database contains invalid JSON."""
        # Test 1 logic

    async def test_error_on_missing_required_field(tmp_path, test_agent):
        """Test validation error when required field is missing."""
        # Test 2 logic

    async def test_get_nonexistent_call_returns_none(tmp_path, test_agent):
        """Test that getting non-existent call_id returns None."""
        # Test 3 logic

    async def test_error_on_malformed_timestamp(tmp_path, test_agent):
        """Test error handling for malformed timestamps."""
        # Test 4 logic
    ```

    **Benefits:**
    - Each test runs independently (failure isolation)
    - Clear test names in pytest output
    - Can run individual tests: `pytest -k test_calltoolresult_image_content`
    - Better coverage reporting (shows exactly which cases pass/fail)
    - Follows pytest best practices
  |||,
}
