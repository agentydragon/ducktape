local I = import '../../specimens/lib.libsonnet';

// iss-020: Inline adapter and delete get_json_schema helper

I.issueOneOccurrence(
  rationale=|||
    The `get_json_schema` helper function (lines 183-186) has exactly one call site and should
    be inlined and deleted.

    **Current code:**
    ```python
    def get_json_schema(model: type) -> dict[str, Any]:
        """Get JSON Schema for a Pydantic model."""
        adapter = TypeAdapter(model)
        return adapter.json_schema(mode="serialization")
    ```

    Called at line 250: `schema = get_json_schema(model_class)`

    **Should be inlined to:**
    ```python
    # At line 250:
    schema = TypeAdapter(model_class).json_schema(mode="serialization")
    ```

    **Why inline:**
    - Single call site at line 250 (within `generate_pydantic_types`)
    - Simple 2-line function with no complex logic
    - The `adapter` intermediate variable is only used once, so it should be inlined too
    - Function name doesn't add semantic clarity beyond the chained method call
    - Reduces indirection - the inline version is just as readable
    - TypeAdapter is already imported, so no additional imports needed

    **After inlining:**
    Delete the `get_json_schema` function entirely (lines 183-186).
  |||,
  properties=['code-style', 'simplicity', 'dead-code'],
  filesToRanges={
    'adgn/scripts/generate_frontend_code.py': [
      [183, 186],  // get_json_schema function - should be deleted
      [250, 250],  // Single call site - inline here
    ],
  },
)
