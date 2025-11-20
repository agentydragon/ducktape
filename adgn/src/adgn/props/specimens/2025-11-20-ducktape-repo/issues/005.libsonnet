local I = import '../../specimens/lib.libsonnet';

// iss-005: Unnecessary intermediate variables before assertions

I.issueOneOccurrence(
  rationale=|||
    The test file extracts values from lists into intermediate variables (fc1, fco1, fc2, fco2)
    and then immediately uses them once in assertions. These should be inlined directly into
    the assert_that() calls.

    **Examples:**

    Lines 89-94:
    ```python
    fc1 = turn2_input[ri1_idx + 1]
    assert_that(
        fc1,
        is_function_call_item(call_id="call_1", id="fc_id_1", status="completed"),
        f"Turn 2: FC1 fields not preserved or wrong type: {turn2_types}",
    )
    ```
    Should be:
    ```python
    assert_that(
        turn2_input[ri1_idx + 1],
        is_function_call_item(call_id="call_1", id="fc_id_1", status="completed"),
        f"Turn 2: FC1 fields not preserved or wrong type: {turn2_types}",
    )
    ```

    Lines 96-101: `fco1 = turn2_input[ri1_idx + 2]` → inline
    Lines 113-114: `fc1 = turn3_input[ri1_idx + 1]` → inline
    Lines 115-116: `fco1 = turn3_input[ri1_idx + 2]` → inline
    Lines 124-129: `fc2 = turn3_input[ri2_idx + 1]` → inline
    Lines 131-132: `fco2 = turn3_input[ri2_idx + 2]` → inline

    **Why inline?**
    - Variables are used exactly once, immediately after definition
    - No clarifying benefit from the variable name (fc1/fco1 vs turn2_input[ri1_idx + 1])
    - Less code to read and maintain
    - Standard pattern: only extract to variable if used multiple times or if expression
      is complex and variable name adds clarity

    **Note:** Lines 19-32 define fc1 and fc2 for constructing the response sequence.
    Those are fine - they're used in sequence construction, not single-use assertion variables.
  |||,
  properties=['code-style', 'readability', 'dry-principle'],
  filesToRanges={
    'adgn/tests/agent/test_reasoning_threading.py': [
      [89, 94],   // fc1 extraction + assertion
      [96, 101],  // fco1 extraction + assertion
      [113, 114], // fc1 extraction + assertion (turn 3)
      [115, 116], // fco1 extraction + assertion (turn 3)
      [124, 129], // fc2 extraction + assertion
      [131, 132], // fco2 extraction + assertion
    ],
  },
)
