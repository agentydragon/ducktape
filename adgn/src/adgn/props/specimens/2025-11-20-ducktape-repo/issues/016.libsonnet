local I = import '../../specimens/lib.libsonnet';

// iss-016: _coerce_error_data is overly defensive, should use model_validate

I.issueOneOccurrence(
  rationale=|||
    The `_coerce_error_data` function (lines 62-93) attempts to coerce various error
    representations to `mtypes.ErrorData` with extensive defensive fallback logic. This
    is a red flag - the function should most likely just use `mtypes.ErrorData.model_validate`
    and let Pydantic handle validation.

    **Current approach (overly defensive):**
    ```python
    def _coerce_error_data(obj: Any) -> mtypes.ErrorData | None:
        if isinstance(obj, mtypes.ErrorData):
            return obj
        if isinstance(obj, dict):
            try:
                return mtypes.ErrorData.model_validate(obj)
            except Exception as e:
                # Fallback: manually construct from code/message
                ...
        # Fallback: extract from object attributes
        if isinstance(obj, _ErrorFields):
            ...
    ```

    **Why this is problematic:**
    - Too much defensive coercion logic trying to handle multiple representations
    - Swallows validation errors and tries manual construction (lines 75-85)
    - Has attribute-based fallback for objects with .code/.message (lines 87-92)
    - Mixes validation concerns with data extraction
    - Makes debugging harder when data doesn't match expected shape
    - Violates fail-fast principle - should let validation errors propagate

    **Recommended approach:**
    Remove `_coerce_error_data` entirely. Callers should use `mtypes.ErrorData.model_validate`
    directly:
    ```python
    # Before:
    error_data = _coerce_error_data(err.error)

    # After:
    error_data = mtypes.ErrorData.model_validate(err.error)
    ```

    If the data doesn't match ErrorData schema, Pydantic will raise a clear validation
    error, which is better than silently constructing minimal ErrorData or returning None.

    **Locations using _coerce_error_data:**
    - Line 116: `error_data = _coerce_error_data(err.error)`
    - Line 119: `error_data = _coerce_error_data(err)`
    - Line 122: `error_data = _coerce_error_data(err.error)`

    All three should be replaced with direct `model_validate` calls (with appropriate
    None-checking if the attribute might not exist).

    **Alternative (if None handling is needed):**
    If callers genuinely need to handle None gracefully:
    ```python
    try:
        error_data = mtypes.ErrorData.model_validate(obj)
    except ValidationError:
        error_data = None
    ```

    But this should be caller's choice, not hidden in a helper function.
  |||,
  properties=['fail-fast', 'validation', 'error-handling', 'simplicity', 'dead-code'],
  filesToRanges={
    'adgn/src/adgn/mcp/policy_gateway/signals.py': [
      [62, 93],   // Entire _coerce_error_data function
      [116, 116], // Usage in detect_policy_gateway_error
      [119, 119], // Usage in detect_policy_gateway_error
      [122, 122], // Usage in detect_policy_gateway_error
    ],
  },
)
