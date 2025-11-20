local I = import '../../specimens/lib.libsonnet';

// iss-013: Message types should use wrapper pattern with kind discriminator

I.issueOneOccurrence(
  rationale=|||
    The input message types (`AssistantMessage`, `UserMessage`, `SystemMessage`) embed the
    discriminator field (`role`) directly in the message class. This mixes API-level concerns
    with the message content structure.

    **Current structure (lines 26-29):**
    ```python
    class AssistantMessage(BaseModel):
        role: Literal["assistant"] = "assistant"
        content: list[InputTextPart] | None = None
    ```

    **Desired structure:**
    The message should be separated from its discriminator using a wrapper pattern:
    ```python
    class AssistantMessage(BaseModel):
        content: list[InputTextPart] | None = None

    class AssistantMessageIn(BaseModel):
        kind: Literal["assistant_message"] = "assistant_message"
        message: AssistantMessage
    ```

    Or following the output pattern, if keeping fields flat:
    ```python
    AssistantMessageOut = {
        kind: "assistant_message",
        assistant_message: AssistantMessage
    }
    ```

    **Why separate?**
    - Consistent discriminator naming (`kind` vs `role` vs `type` - currently mixed)
    - Separates transport/API concerns from message content structure
    - Similar to how `AssistantMessageOut` uses `kind` discriminator
    - Enables clearer type discrimination for union types (InputItem)
    - Message content can evolve independently from serialization format

    **Current inconsistency:**
    - Input messages use `role` as discriminator
    - Other input items use `type` as discriminator (ReasoningItem, FunctionCallItem)
    - Output messages use `kind` as discriminator (AssistantMessageOut)

    This should be unified using wrapper pattern with consistent `kind` discriminators.
  |||,
  properties=['api-design', 'type-safety', 'consistency', 'maintainability'],
  filesToRanges={
    'adgn/src/adgn/openai_utils/model.py': [
      [26, 33],   // AssistantMessage with role discriminator
      [36, 43],   // UserMessage with role discriminator
      [46, 53],   // SystemMessage with role discriminator
      [93, 93],   // InputItem union - would benefit from consistent discriminators
      [172, 182], // AssistantMessageOut uses kind discriminator (reference pattern)
    ],
  },
)
