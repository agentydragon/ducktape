local I = import '../../specimens/lib.libsonnet';

// iss-008: Complex nested loop assertion should use hamcrest matchers

I.issueOneOccurrence(
  rationale=|||
    A complex 12-line assertion with nested loops, boolean flag, and break statements
    should be replaced with a single declarative hamcrest matcher.

    **Current code (lines 136-147):**
    ```python
    assert_that(captured, has_length(greater_than_or_equal_to(2)), "expected at least two sampling calls")
    second = captured[-1]
    found = False
    for msg in second.input or []:
        if isinstance(msg, UserMessage):
            for c in msg.content or []:
                if isinstance(c, InputTextPart) and "<system notification>" in c.text:
                    found = True
                    break
        if found:
            break
    assert found, "expected system notification after tool-triggered update"
    ```

    **What it checks:**
    1. captured has at least 2 items
    2. Last item (captured[-1]) has input field
    3. Input contains a UserMessage
    4. UserMessage has content containing an InputTextPart
    5. InputTextPart.text contains substring "<system notification>"

    **Should be (single hamcrest assertion):**
    ```python
    assert_that(
        captured,
        all_of(
            has_length(greater_than_or_equal_to(2)),
            has_item(has_properties(
                input=has_item(all_of(
                    instance_of(UserMessage),
                    has_properties(content=has_item(all_of(
                        instance_of(InputTextPart),
                        has_properties(text=contains_string("<system notification>"))
                    )))
                ))
            )),
        ),
        "expected at least two sampling calls with system notification"
    )
    ```

    Or more concisely, checking just the last element:
    ```python
    assert_that(captured, has_length(greater_than_or_equal_to(2)))
    assert_that(
        captured[-1].input,
        has_item(all_of(
            instance_of(UserMessage),
            has_properties(content=has_item(all_of(
                instance_of(InputTextPart),
                has_properties(text=contains_string("<system notification>"))
            )))
        )),
        "expected system notification in last sampling call"
    )
    ```

    **Benefits:**
    - Declarative vs imperative (describes what, not how)
    - No manual loops, flags, or breaks
    - Better error messages (hamcrest shows full object diff)
    - More readable and maintainable
    - Consistent with hamcrest usage elsewhere in tests
    - Eliminates mutable state (found flag)
  |||,
  properties=['test-quality', 'hamcrest-matchers', 'readability', 'declarative-style'],
  filesToRanges={
    'adgn/tests/agent/test_mcp_notifications_flow.py': [
      [136, 147],  // Complex nested loop assertion
    ],
  },
)
