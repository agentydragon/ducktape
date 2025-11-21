local I = import '../../specimens/lib.libsonnet';

// iss-026: Use f-string debug format {agent_id=} instead of manual interpolation

I.issueOneOccurrence(
  rationale=|||
    The logger.debug call manually constructs the debug message when f-string debug format
    is more concise.

    **Current code (line 99):**
    ```python
    logger.debug(f"Authenticated request: token → agent_id={agent_id}")
    ```

    **Should be:**
    ```python
    logger.debug(f"Authenticated request: {agent_id=}")
    ```

    **Why f-string debug format is better:**
    - More concise: no need to repeat "agent_id=" twice
    - Self-documenting: shows both variable name and value
    - Standard Python 3.8+ debugging pattern
    - Less error-prone: can't accidentally mismatch variable name in string

    **Example output:**
    - Current: `Authenticated request: token → agent_id=chatgpt-agent`
    - Proposed: `Authenticated request: agent_id='chatgpt-agent'`

    The debug format automatically adds quotes around string values, making it clearer
    what the actual value is (especially useful for empty strings, whitespace, etc.).

    **Note:** The "token →" prefix doesn't add value since we're not logging the actual
    token (for security reasons). The {agent_id=} format is sufficient.
  |||,
  properties=['code-style', 'readability'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/auth.py': [
      [99, 99],  // logger.debug with manual interpolation
    ],
  },
)
