{
  title: 'Duplicated Bearer token extraction logic should be unified',
  severity: 'medium',
  category: 'code-duplication',
  locations: [
    {
      path: 'adgn/src/adgn/agent/mcp_bridge/auth.py',
      lines: [75, 91],
      context: 'TokenAuthMiddleware extracts Bearer token',
    },
    {
      path: 'adgn/src/adgn/agent/mcp_bridge/auth.py',
      lines: [144, 161],
      context: 'UITokenAuthMiddleware extracts Bearer token',
    },
  ],
  description: |||
    Both TokenAuthMiddleware and UITokenAuthMiddleware duplicate the same
    Bearer token extraction logic:

    **TokenAuthMiddleware.dispatch() (lines 75-91):**
    ```python
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format (expected: Bearer <token>)",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = parts[1]
    ```

    **UITokenAuthMiddleware.__call__() (lines 144-161):**
    ```python
    auth_header = headers.get(b"authorization", b"").decode()

    error_response = None
    if not auth_header:
        error_response = self._create_error_response(
            401, "Missing Authorization header"
        )
    else:
        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            error_response = self._create_error_response(
                401, "Invalid Authorization header format (expected: Bearer <token>)"
            )
        elif parts[1] != self.expected_token:
            error_response = self._create_error_response(
                401, "Invalid token"
            )
    ```

    Both implementations:
    1. Check if Authorization header exists
    2. Split on whitespace
    3. Validate format is "Bearer <token>"
    4. Extract the token (parts[1])

    This is classic code duplication.

    **Additionally:** FastMCP likely has better patterns for authentication middleware
    that should be investigated. Most web frameworks provide standardized auth helpers
    to avoid exactly this kind of duplication.
  |||,
  recommendation: |||
    **Option 1: Extract a shared helper function**

    ```python
    def extract_bearer_token(auth_header: str | None) -> tuple[str | None, dict | None]:
        """Extract Bearer token from Authorization header.

        Returns:
            Tuple of (token, error_dict). If token is None, error_dict contains error details.
        """
        if not auth_header:
            return None, {
                "status": 401,
                "detail": "Missing Authorization header",
                "headers": {"WWW-Authenticate": "Bearer"},
            }

        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None, {
                "status": 401,
                "detail": "Invalid Authorization header format (expected: Bearer <token>)",
                "headers": {"WWW-Authenticate": "Bearer"},
            }

        return parts[1], None
    ```

    Then both middleware classes use this helper.

    **Option 2 (Preferred): Use FastMCP's authentication patterns**

    Investigate if FastMCP provides:
    - Built-in Bearer token middleware
    - Authentication dependency injection
    - Standard auth utilities

    Most modern Python web frameworks (FastAPI, Starlette, etc.) provide standardized
    auth patterns. If FastMCP builds on these, use the provided patterns instead of
    rolling custom middleware.

    **Option 3: Consolidate middleware**

    If both middleware are doing the same thing (Bearer token validation), consider
    whether they should be a single parameterized middleware:

    ```python
    class BearerTokenMiddleware:
        def __init__(self, app, token_validator: Callable[[str], AgentID | bool]):
            """token_validator returns AgentID for multi-tenant, bool for single-tenant"""
            self.app = app
            self.token_validator = token_validator
    ```

    This eliminates the duplication entirely by unifying the two use cases.
  |||,
}
