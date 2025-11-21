local I = import '../../specimens/lib.libsonnet';

// iss-025: Simplify TokenMapping loading with Pydantic TypeAdapter

I.issueOneOccurrence(
  rationale=|||
    The `reload()` method manually parses JSON and validates the dict[str, str] structure
    when Pydantic's TypeAdapter can do this more cleanly and with better error messages.

    **Current code (lines 44-56):**
    ```python
    data = json.loads(self.path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Token mapping must be a JSON object")

    # Validate all values are strings and convert to AgentID
    mapping: dict[str, AgentID] = {}
    for token, agent_id in data.items():
        if not isinstance(token, str) or not isinstance(agent_id, str):
            raise ValueError(f"Invalid mapping: {token} -> {agent_id}")
        mapping[token] = AgentID(agent_id)

    self._mapping = mapping
    logger.info(f"Loaded {len(self._mapping)} token mappings from {self.path}")
    ```

    **Should be (using TypeAdapter):**
    ```python
    from pydantic import TypeAdapter

    adapter = TypeAdapter(dict[str, str])
    data = adapter.validate_json(self.path.read_text())
    self._mapping = {token: AgentID(agent_id) for token, agent_id in data.items()}
    logger.info(f"Loaded {len(self._mapping)} token mappings from {self.path}")
    ```

    **Why TypeAdapter is better:**
    - **Cleaner code**: 3 lines instead of 10
    - **Better validation**: Pydantic provides detailed validation errors with locations
    - **JSON parsing integrated**: `validate_json()` handles both parsing and validation
    - **Type safety**: TypeAdapter ensures dict[str, str] structure at runtime
    - **Better error messages**: Instead of "Invalid mapping: foo -> 123", get:
      ```
      ValidationError: 1 validation error for dict[str, str]
      value.foo
        Input should be a valid string [type=string_type, input_value=123]
      ```
    - **No manual isinstance checks**: Pydantic handles all type checking
    - **No manual loop**: Dict comprehension is more Pythonic than imperative loop

    **Current problems with manual validation:**
    - Lines 45-46: Manual dict check duplicates what Pydantic does
    - Lines 50-52: Manual isinstance checks are verbose and error-prone
    - Line 52: Generic error message doesn't say WHAT is wrong (is token not a string? is agent_id not a string?)
    - Line 44: Requires separate `json` import when Pydantic can handle JSON directly

    **Note on AgentID:**
    `AgentID` is a `NewType("AgentID", str)` (types.py:7), so it's essentially `str`.
    The dict comprehension converts str → AgentID, which is safe and preserves type safety.

    **Possible further simplification:**
    If Pydantic's TypeAdapter supports NewType directly (needs testing), could use:
    ```python
    adapter = TypeAdapter(dict[str, AgentID])
    self._mapping = adapter.validate_json(self.path.read_text())
    ```
    But the dict comprehension version is safer and more explicit.

    **Imports to add:**
    - Line 11: Can remove `import json` if it's not used elsewhere
    - Need to add: `from pydantic import TypeAdapter`
  |||,
  properties=['simplicity', 'validation', 'error-messages', 'type-safety'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/auth.py': [
      [44, 55],  // Manual JSON parsing and validation loop
      [10, 10],  // import json - may be removable
    ],
  },
)
