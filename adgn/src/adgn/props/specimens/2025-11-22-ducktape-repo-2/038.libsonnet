{
  title: 'Use f"{variable=}" syntax instead of f"variable={variable}"',
  severity: 'minor',
  category: 'code-style',
  locations: [
    {
      path: 'adgn/src/adgn/agent/mcp_bridge/auth.py',
      lines: [99],
      context: 'logger.debug(f"Authenticated request: token → agent_id={agent_id}")',
    },
    {
      path: 'adgn/src/adgn/agent/mcp_bridge/server.py',
      lines: [122],
      context: 'logger.info(f"Infrastructure ready for agent_id={agent_id}")',
    },
  ],
  description: |||
    Python 3.8+ supports f"{variable=}" syntax which is more concise than f"variable={variable}":

    **auth.py line 99:**
    ```python
    logger.debug(f"Authenticated request: token → agent_id={agent_id}")
    ```

    **server.py line 122:**
    ```python
    logger.info(f"Infrastructure ready for agent_id={agent_id}")
    ```

    Both can be shortened using the = suffix in f-strings.
  |||,
  recommendation: |||
    Use f"{variable=}" syntax:

    **auth.py:**
    ```python
    logger.debug(f"Authenticated request: token → {agent_id=}")
    ```

    **server.py:**
    ```python
    logger.info(f"Infrastructure ready for {agent_id=}")
    ```

    This is more concise and makes it clear we're debugging/logging a variable's value.
  |||,
}
