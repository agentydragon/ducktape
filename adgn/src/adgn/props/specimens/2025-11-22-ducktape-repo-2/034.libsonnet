local I = import '../../specimens/lib.libsonnet';

// iss-034: Variable assignments should use walrus operator in conditionals

I.issueOneOccurrence(
  rationale= |||
    Three places assign a variable and immediately check it in a conditional.
    These should use the walrus operator (:=) to combine assignment and test.

    TokenAuthMiddleware.dispatch() (lines 75-76):
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(...)

    generate_ui_token() (lines 113-114):
    env_token = os.environ.get("ADGN_UI_TOKEN")
    if env_token:
        logger.info("Using ADGN_UI_TOKEN from environment")
        return env_token

    UITokenAuthMiddleware.__call__() (lines 144-148):
    auth_header = headers.get(b"authorization", b"").decode()
    # Validate authentication
    error_response = None
    if not auth_header:
        error_response = self._create_error_response(...)

    All three cases assign and then immediately test the value - perfect for walrus.

    Fix: Use walrus operator (:=) to combine assignment and conditional test:

    if not (auth_header := request.headers.get("Authorization")):
        raise HTTPException(...)

    if env_token := os.environ.get("ADGN_UI_TOKEN"):
        logger.info("Using ADGN_UI_TOKEN from environment")
        return env_token

    if not (auth_header := headers.get(b"authorization", b"").decode()):
        error_response = self._create_error_response(...)

    Benefits:
    - More concise (one line instead of two)
    - Clear intent (we're testing the retrieved value)
    - Modern Python idiom (walrus operator introduced in Python 3.8)
    - Variable scope is explicit (only exists where needed)
  |||,
  properties=['python/walrus'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/auth.py': [
      [75, 76],   // TokenAuthMiddleware.dispatch(): auth_header + if
      [113, 114], // generate_ui_token(): env_token + if
      [144, 148], // UITokenAuthMiddleware.__call__(): auth_header + if
    ],
  },
)
