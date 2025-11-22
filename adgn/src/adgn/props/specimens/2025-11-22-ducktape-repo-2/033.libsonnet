{
  title: 'total_tokens field is redundant - trivial sum of input_tokens + output_tokens',
  severity: 'minor',
  category: 'data-modeling',
  locations: [
    {
      path: 'adgn/src/adgn/agent/handler.py',
      lines: [29],
      context: 'total_tokens field in TokenUsage model',
    },
  ],
  description: |||
    The TokenUsage model has a `total_tokens` field that is a trivial sum of two other fields:

    ```python
    class TokenUsage(BaseModel):
        input_tokens: int | None = Field(None, description="Input tokens consumed")
        output_tokens: int | None = Field(None, description="Output tokens generated")
        total_tokens: int | None = Field(None, description="Total tokens consumed (input + output)")
    ```

    The `total_tokens` field is redundant:
    - It's always `input_tokens + output_tokens`
    - No additional information
    - Must be kept in sync manually (error-prone)
    - Wastes storage/bandwidth

    This violates DRY - the total is trivially computable from the parts.
  |||,
  recommendation: |||
    **Remove the total_tokens field entirely.**

    Callers who need the total can compute it trivially:
    ```python
    total = (usage.input_tokens or 0) + (usage.output_tokens or 0)
    ```

    **If keeping for API compatibility, make it a computed property:**
    ```python
    class TokenUsage(BaseModel):
        input_tokens: int | None = Field(None, description="Input tokens consumed")
        output_tokens: int | None = Field(None, description="Output tokens generated")

        @property
        def total_tokens(self) -> int | None:
            """Total tokens (input + output). Returns None if both are None."""
            if self.input_tokens is None and self.output_tokens is None:
                return None
            return (self.input_tokens or 0) + (self.output_tokens or 0)
    ```

    This ensures:
    - Single source of truth (input + output)
    - Cannot get out of sync
    - No redundant storage
    - Backward compatible if needed
  |||,
}
