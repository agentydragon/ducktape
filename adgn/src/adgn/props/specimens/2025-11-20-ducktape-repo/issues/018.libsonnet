local I = import '../../specimens/lib.libsonnet';

// iss-018: Delete unused dead code functions (0 call sites)

I.issueOneOccurrence(
  rationale=|||
    Three functions have zero call sites in the codebase and should be deleted as dead code.

    **1. `AssistantMessageOut.from_input_item` (model.py:219-225)**
    ```python
    @classmethod
    def from_input_item(cls, item: AssistantMessage) -> AssistantMessageOut:
        parts: list[OutputText] = []
        for block in item.content or []:
            if isinstance(block, InputTextPart):
                parts.append(OutputText.model_validate(block.model_dump(exclude_none=True)))
        return cls(parts=parts)
    ```

    - **Call sites**: ZERO
    - **Purpose**: Converts AssistantMessage (input) back to AssistantMessageOut (output)
    - **Why dead**: This reverse conversion is never used. The normal flow is input → processing → output, not output → input
    - **Action**: Delete this classmethod

    **2. `ResponsesResult.to_input_items` (model.py:280-281)**
    ```python
    def to_input_items(self) -> list[InputItem]:
        return [response_out_item_to_input(item) for item in self.output]
    ```

    - **Call sites**: ZERO
    - **Purpose**: Converts response output items back to input items
    - **Why dead**: This method depends on `response_out_item_to_input`, which itself has only 1 caller (in this method). The entire conversion-back path is unused.
    - **Related**: `response_out_item_to_input` singledispatch (lines 231-253) is only called from this dead method, so it should also be evaluated for deletion
    - **Action**: Delete this method

    **3. `detect_policy_gateway_error` (signals.py:96-145)**
    ```python
    def detect_policy_gateway_error(
        err: FastMcpCallToolResult | mtypes.CallToolResult | McpError | dict[str, Any] | mtypes.ErrorData | BaseException,
    ) -> PolicyGatewayError | None:
        """Detect and classify policy-gateway errors robustly.
        ...
        NOTE: This function is currently unused in the codebase.
        """
    ```

    - **Call sites**: ZERO
    - **Purpose**: Detects and classifies policy gateway errors from various error types
    - **Why dead**: Already documented as unused at line 110: "NOTE: This function is currently unused in the codebase."
    - **Size**: 50 lines of complex error detection logic
    - **Action**: Delete this function unless it's planned for future use. If keeping for future use, it should be moved to a "planned features" module or explicitly documented as such.

    **Cleanup strategy:**
    1. Delete `AssistantMessageOut.from_input_item` (model.py:219-225)
    2. Delete `ResponsesResult.to_input_items` (model.py:280-281)
    3. Evaluate `response_out_item_to_input` singledispatch (model.py:231-253) - if only called from deleted method, delete it too
    4. Delete `detect_policy_gateway_error` (signals.py:96-145) or move to future-features module

    **Benefits:**
    - Reduces maintenance burden
    - Eliminates confusion about unused code paths
    - Improves code readability by removing noise
    - Can always restore from git history if needed
  |||,
  properties=['dead-code', 'maintainability', 'clarity'],
  filesToRanges={
    'adgn/src/adgn/openai_utils/model.py': [
      [219, 225],  // AssistantMessageOut.from_input_item - dead code
      [280, 281],  // ResponsesResult.to_input_items - dead code
      [231, 253],  // response_out_item_to_input - only called from dead method
    ],
    'adgn/src/adgn/mcp/policy_gateway/signals.py': [
      [96, 145],   // detect_policy_gateway_error - documented as unused
      [110, 110],  // Line with "NOTE: This function is currently unused"
    ],
  },
)
