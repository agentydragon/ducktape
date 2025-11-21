local I = import '../../specimens/lib.libsonnet';

// iss-020: PolicyError index and length fields are ambiguous and should be removed

I.issueOneOccurrence(
  rationale=|||
    The `index` and `length` fields in `PolicyError` claim to indicate where an error occurred, but
    they don't specify whether they're character indices or token indices, making them useless.
    These fields should be removed as they're not currently necessary.

    **Current code (lines 23-24):**
    ```python
    index: int | None = Field(None, description="Character/token index where error occurred")
    length: int | None = Field(None, description="Length of error span in characters/tokens")
    ```

    **Why this is problematic:**

    1. **Ambiguous semantics**: The description "Character/token index" doesn't specify which one it is.
       - Is `index=10` the 10th character or the 10th token?
       - If it's both (depending on context), how does the consumer know which?
       - No way to distinguish makes the field meaningless

    2. **Same ambiguity for length**: "Length of error span in characters/tokens" has the same problem.
       - Is `length=5` five characters or five tokens?
       - Can't be used reliably without knowing the unit

    3. **Not currently necessary**: There's no evidence these fields are being populated or consumed
       anywhere in the codebase. They provide unnecessary detail.

    4. **No standardization**: Different error sources might use different interpretations (characters
       vs tokens), making the field inconsistent and unreliable.

    **Recommended fix:**

    Remove both `index` and `length` fields entirely:

    ```python
    class PolicyError(BaseModel):
        stage: PolicyErrorStage = Field(description="Processing stage where error occurred")
        code: PolicyErrorCode = Field(description="Error code (read_error, parse_error)")
        message: str | None = Field(None, description="Human-readable error message")

        model_config = ConfigDict(extra="forbid")
    ```

    **Benefits:**
    - Eliminates ambiguous fields that can't be used reliably
    - Simpler model with only the essential error information
    - `message` field can include location details if needed (e.g., "Parse error at line 5, column 10")
    - Removes unused fields that add complexity without value
    - Clearer that the error model focuses on stage, code, and message

    **Alternative (if location is truly needed in the future):**
    Define separate fields with unambiguous semantics:
    ```python
    line: int | None = Field(None, description="Line number where error occurred")
    column: int | None = Field(None, description="Column number where error occurred")
    char_index: int | None = Field(None, description="Character index where error occurred")
    char_length: int | None = Field(None, description="Length of error span in characters")
    ```

    But the current level of detail (stage, code, message) is sufficient for error reporting.

    **Note:**
    If these fields are being populated somewhere, that code should also be removed as part of this fix.
  |||,
  properties=['api-design', 'clarity', 'unused-fields'],
  filesToRanges={
    'adgn/src/adgn/agent/models/policy_error.py': [
      [23, 24],  // Ambiguous index and length fields
    ],
  },
)
