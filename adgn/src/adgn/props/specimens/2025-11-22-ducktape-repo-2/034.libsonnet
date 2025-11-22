{
  title: 'Variable assignments should use walrus operator in conditionals',
  severity: 'minor',
  category: 'code-style',
  locations: [
    {
      path: 'adgn/src/adgn/agent/mcp_bridge/auth.py',
      lines: [75, 76],
      context: 'auth_header = request.headers.get(...); if not auth_header:',
    },
    {
      path: 'adgn/src/adgn/agent/mcp_bridge/auth.py',
      lines: [113, 114],
      context: 'env_token = os.environ.get(...); if env_token:',
    },
    {
      path: 'adgn/src/adgn/agent/mcp_bridge/auth.py',
      lines: [144, 148],
      context: 'auth_header = headers.get(...); if not auth_header:',
    },
  ],
  description: |||
    Several places assign a variable and immediately check it in a conditional.
    These should use the walrus operator (:=) to combine assignment and test.

    **TokenAuthMiddleware.dispatch() - lines 75-76:**
    ```python
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(...)
    ```

    **generate_ui_token() - lines 113-114:**
    ```python
    env_token = os.environ.get("ADGN_UI_TOKEN")
    if env_token:
        logger.info("Using ADGN_UI_TOKEN from environment")
        return env_token
    ```

    **UITokenAuthMiddleware.__call__() - lines 144-148:**
    ```python
    auth_header = headers.get(b"authorization", b"").decode()

    # Validate authentication
    error_response = None
    if not auth_header:
        error_response = self._create_error_response(...)
    ```

    All three cases assign and then immediately test the value - perfect for walrus.
  |||,
  recommendation: |||
    Use walrus operator (:=) to combine assignment and conditional test:

    **TokenAuthMiddleware.dispatch():**
    ```python
    if not (auth_header := request.headers.get("Authorization")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    ```

    **generate_ui_token():**
    ```python
    if env_token := os.environ.get("ADGN_UI_TOKEN"):
        logger.info("Using ADGN_UI_TOKEN from environment")
        return env_token
    ```

    **UITokenAuthMiddleware.__call__():**
    ```python
    # Validate authentication
    error_response = None
    if not (auth_header := headers.get(b"authorization", b"").decode()):
        error_response = self._create_error_response(
            401, "Missing Authorization header"
        )
    ```

    **Benefits:**
    - More concise (one line instead of two)
    - Clear intent (we're testing the retrieved value)
    - Modern Python idiom (walrus operator introduced in Python 3.8)
    - Variable scope is explicit (only exists where needed)
  |||,
}
