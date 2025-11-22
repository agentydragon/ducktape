local I = import '../../specimens/lib.libsonnet';

// iss-038: use f"{variable=}" debug syntax

I.issueOneOccurrence(
  rationale= |||
    Python 3.8+ supports f"{variable=}" syntax which is more concise than f"variable={variable}":

    auth.py line 99:
    ```python
    logger.debug(f"Authenticated request: token → agent_id={agent_id}")
    ```

    server.py line 122:
    ```python
    logger.info(f"Infrastructure ready for agent_id={agent_id}")
    ```

    Both can be shortened using the = suffix in f-strings.

    Use f"{variable=}" syntax:

    auth.py:
    ```python
    logger.debug(f"Authenticated request: token → {agent_id=}")
    ```

    server.py:
    ```python
    logger.info(f"Infrastructure ready for {agent_id=}")
    ```

    This is more concise and makes it clear we're debugging/logging a variable's value.
  |||,
  properties=['python/modern-python-idioms'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/auth.py': [
      99,
    ],
    'adgn/src/adgn/agent/mcp_bridge/server.py': [
      122,
    ],
  },
)
