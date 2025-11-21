local I = import '../../specimens/lib.libsonnet';

// iss-027: Use walrus operator for env_token check

I.issueOneOccurrence(
  rationale=|||
    The code extracts env_token and then immediately checks it in a separate if statement.
    This should use the walrus operator to inline the assignment.

    **Current code (lines 113-115):**
    ```python
    env_token = os.environ.get("ADGN_UI_TOKEN")
    if env_token:
        logger.info("Using ADGN_UI_TOKEN from environment")
        return env_token
    ```

    **Should be:**
    ```python
    if env_token := os.environ.get("ADGN_UI_TOKEN"):
        logger.info("Using ADGN_UI_TOKEN from environment")
        return env_token
    ```

    **Why walrus operator is better:**
    - Combines retrieval and check into one line
    - Standard Python 3.8+ pattern for "get and check" scenarios
    - More concise without sacrificing readability
    - Variable scope is correctly limited to the if block and its body
    - Consistent with other walrus operator uses in the codebase

    **Pattern applicability:**
    This is a textbook case for walrus operator: we get a value, check if it's truthy,
    and use it in the if body. No need for separate assignment.
  |||,
  properties=['code-style', 'readability'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/auth.py': [
      [113, 116],  // env_token extraction and check
    ],
  },
)
