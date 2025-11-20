local I = import '../../specimens/lib.libsonnet';

// iss-009: Unnecessary intermediate variables before single-use assertions

I.issueOneOccurrence(
  rationale=|||
    Multiple test files extract values into variables and then immediately use them once
    in an assertion. These should be inlined directly into the assertion.

    **test_loop_reducer_skip_sampling.py (2 instances):**

    Lines 24-31:
    ```python
    dec = ctrl.on_before_sample()
    assert_that(
        dec,
        all_of(
            instance_of(Continue),
            has_properties(tool_policy=instance_of(Auto), skip_sampling=True, inserts_input=(m1, m2)),
        ),
    )
    ```

    Should be:
    ```python
    assert_that(
        ctrl.on_before_sample(),
        all_of(
            instance_of(Continue),
            has_properties(tool_policy=instance_of(Auto), skip_sampling=True, inserts_input=(m1, m2)),
        ),
    )
    ```

    Lines 38-45: Identical pattern with different test data

    **test_aggregating_inserts.py (1 instance):**

    Lines 35-42:
    ```python
    dec = ctrl.on_before_sample()
    assert_that(
        dec,
        all_of(
            instance_of(Continue),
            has_properties(tool_policy=instance_of(Auto), inserts_input=has_length(2)),
        ),
    )
    ```

    Should be:
    ```python
    assert_that(
        ctrl.on_before_sample(),
        all_of(
            instance_of(Continue),
            has_properties(tool_policy=instance_of(Auto), inserts_input=has_length(2)),
        ),
    )
    ```

    **test_exec_roundtrip.py (1 instance):**

    Lines 24-32:
    ```python
    res = await stub(ExecInput(cmd=ECHO_CMD, timeout_ms=10_000))
    assert_that(
        res,
        has_properties(
            exit=all_of(instance_of(Exited), has_properties(exit_code=0)),
            stdout="hello",
            stderr="",
        ),
    )
    ```

    Should be:
    ```python
    assert_that(
        await stub(ExecInput(cmd=ECHO_CMD, timeout_ms=10_000)),
        has_properties(
            exit=all_of(instance_of(Exited), has_properties(exit_code=0)),
            stdout="hello",
            stderr="",
        ),
    )
    ```

    **test_editor_inproc.py (1 instance):**

    Lines 29-30:
    ```python
    done_result = await stub.done(DoneInput(outcome=EditorOutcome.SUCCESS, summary=None))
    assert_that(done_result, all_of(instance_of(Success), has_properties(kind="Success")))
    ```

    Should be:
    ```python
    assert_that(
        await stub.done(DoneInput(outcome=EditorOutcome.SUCCESS, summary=None)),
        all_of(instance_of(Success), has_properties(kind="Success"))
    )
    ```

    **Why inline?**
    - Variables are used exactly once, immediately after definition
    - No clarifying benefit from variable name (dec, res vs the call itself)
    - Less code to read and maintain
    - Standard pattern: only extract to variable if:
      - Used multiple times
      - Variable name adds semantic clarity
      - Expression is very complex and variable simplifies reading

    In these cases, the variable names don't add clarity and the expressions are
    straightforward function/method calls.
  |||,
  properties=['code-style', 'readability', 'dry-principle'],
  filesToRanges={
    'adgn/tests/agent/test_loop_reducer_skip_sampling.py': [
      [24, 31],  // dec extraction + assertion (test 1)
      [38, 45],  // dec extraction + assertion (test 2)
    ],
    'adgn/tests/agent/test_aggregating_inserts.py': [
      [35, 42],  // dec extraction + assertion
    ],
    'adgn/tests/agent/test_exec_roundtrip.py': [
      [24, 32],  // res extraction + assertion
    ],
    'adgn/tests/agent/test_editor_inproc.py': [
      [29, 30],  // done_result extraction + assertion
    ],
  },
)
