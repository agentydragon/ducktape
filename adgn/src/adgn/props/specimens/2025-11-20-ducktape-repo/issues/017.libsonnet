local I = import '../../specimens/lib.libsonnet';

// iss-017: PolicyGatewayError.data field needs proper typing and documentation

I.issueOneOccurrence(
  rationale=|||
    The `PolicyGatewayError` model (lines 33-37) has a `data` field with type
    `dict[str, Any] | None`, which is too vague. The field should be renamed and/or
    typed and/or documented to explain **what exact things** get put in there.

    **Current code (lines 33-37):**
    ```python
    class PolicyGatewayError(BaseModel):
        kind: PolicyGatewayErrorKind
        code: int | None = None
        message: str
        data: dict[str, Any] | None = None
    ```

    **Why this is problematic:**
    - Field name "data" is generic and uninformative
    - Type `dict[str, Any]` provides no guidance on structure or contents
    - No documentation explaining what data gets stored here
    - Looking at usage (line 136), it's checked for POLICY_GATEWAY_STAMP_KEY, but that's
      not reflected in the type or name
    - Unclear what other fields might be in the dict besides the stamp key

    **Questions to answer:**
    1. What exactly gets stored in this field?
    2. Is it always just `{POLICY_GATEWAY_STAMP_KEY: True}` or are there other fields?
    3. Should this be a typed model instead of a raw dict?
    4. Is this field actually used beyond the stamp check?

    **Potential solutions:**

    **Option 1: Create a typed model**
    ```python
    class PolicyGatewayErrorData(BaseModel):
        # POLICY_GATEWAY_STAMP_KEY constant value = "_policy_gateway_stamp"
        _policy_gateway_stamp: bool = Field(alias="_policy_gateway_stamp")
        # Add other fields as they're discovered

    class PolicyGatewayError(BaseModel):
        kind: PolicyGatewayErrorKind
        code: int | None = None
        message: str
        error_data: PolicyGatewayErrorData | None = None
    ```

    **Option 2: At minimum, add documentation**
    ```python
    class PolicyGatewayError(BaseModel):
        kind: PolicyGatewayErrorKind
        code: int | None = None
        message: str
        data: dict[str, Any] | None = Field(
            default=None,
            description=(
                "Error metadata from MCP ErrorData. Must contain "
                f"{{POLICY_GATEWAY_STAMP_KEY: True}} to be recognized as a gateway error. "
                "May contain additional fields: [list specific fields here]"
            )
        )
    ```

    **Option 3: Rename to be more specific**
    ```python
    class PolicyGatewayError(BaseModel):
        kind: PolicyGatewayErrorKind
        code: int | None = None
        message: str
        mcp_error_metadata: dict[str, Any] | None = None
    ```

    The code should pick one of these approaches - but NOT just add bullshit documentation
    like "The data associated with the gateway error". Real documentation requires
    understanding what actually gets stored here and documenting those specifics.
  |||,
  properties=['api-design', 'type-safety', 'documentation', 'clarity'],
  filesToRanges={
    'adgn/src/adgn/mcp/policy_gateway/signals.py': [
      [33, 37],   // PolicyGatewayError model with vague 'data' field
      [133, 133], // Line extracting data from error_data
      [136, 136], // Line checking for POLICY_GATEWAY_STAMP_KEY in data
      [142, 142], // Line passing data to PolicyGatewayError constructor
    ],
  },
)
