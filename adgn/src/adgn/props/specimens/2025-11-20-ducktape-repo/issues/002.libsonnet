local I = import '../../specimens/lib.libsonnet';

// iss-002: Unnecessary multi-line formatting for short Pydantic model arguments

I.issueOneOccurrence(
  rationale=|||
    Throughout test_policy_resources.py, short Pydantic model instantiations (2-3 simple
    arguments) are unnecessarily split across multiple lines when they would easily fit
    on one line. This makes the tests more verbose without improving readability.

    **Examples that should be one line:**

    Lines 258-261 (user-mentioned example):
    ```python
    arguments=UpdatePolicyArgs(
        id="nonexistent",
        text="print('new')",
    ).model_dump(),
    ```
    Should be: `arguments=UpdatePolicyArgs(id="nonexistent", text="print('new')").model_dump(),`

    Lines 183-186, 192-195, 205-208: CreatePolicyArgs with 2 args
    Lines 229-233, 239-243: Args with 3 simple string parameters
    Lines 272-275, 281-284, 301-304: Args with 2 simple parameters
    Lines 314, 368-371: Short args split unnecessarily

    **Guideline:**
    - 1-2 arguments: Always one line
    - 3 simple arguments (strings/bools/numbers): Generally one line unless line >100 chars
    - 4+ arguments or complex nested structures: Multi-line acceptable

    Note: Lines 84-89, 94-99, 132-137, 160-165 have 4 arguments and could remain multi-line,
    though they're borderline cases.
  |||,
  properties=['code-style', 'readability', 'consistency'],
  filesToRanges={
    'adgn/tests/mcp/approval_policy/test_policy_resources.py': [
      [183, 186],  // CreatePolicyArgs 2 args
      [192, 195],  // CreatePolicyArgs 2 args
      [205, 208],  // CreatePolicyArgs 2 args
      [229, 233],  // CreatePolicyArgs 3 args
      [239, 243],  // UpdatePolicyArgs 3 args
      [258, 261],  // UpdatePolicyArgs 2 args (user example)
      [272, 275],  // CreatePolicyArgs 2 args
      [281, 284],  // UpdatePolicyArgs 2 args
      [301, 304],  // CreatePolicyArgs 2 args
      [314, 314],  // DeletePolicyArgs 1 arg (already multiline)
      [368, 371],  // CreatePolicyArgs 2 args
    ],
  },
)
