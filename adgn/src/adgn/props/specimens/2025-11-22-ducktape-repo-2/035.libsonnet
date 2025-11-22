local I = import '../../specimens/lib.libsonnet';

// iss-035: Duplicated Bearer token extraction logic should be unified

I.issueOneOccurrence(
  rationale= |||
    Both TokenAuthMiddleware and UITokenAuthMiddleware duplicate the same
    Bearer token extraction logic:

    TokenAuthMiddleware.dispatch() (lines 75-91):
    - Check if Authorization header exists
    - Split on whitespace
    - Validate format is "Bearer <token>"
    - Extract the token (parts[1])

    UITokenAuthMiddleware.__call__() (lines 144-161):
    - Same exact pattern with slightly different error handling

    This is classic code duplication. Both implementations:
    1. Check if Authorization header exists
    2. Split on whitespace
    3. Validate format is "Bearer <token>"
    4. Extract the token (parts[1])

    Fix options:
    1. Extract a shared helper function: extract_bearer_token(auth_header)
       that returns (token | None, error_dict | None)

    2. Preferred: Use FastMCP's authentication patterns if available
       (investigate if FastMCP provides built-in Bearer token middleware,
       authentication dependency injection, or standard auth utilities)

    3. Consolidate middleware: if both are doing the same thing (Bearer
       token validation), consider a single parameterized middleware:
       BearerTokenMiddleware(token_validator: Callable)

    Most modern Python web frameworks (FastAPI, Starlette, etc.) provide
    standardized auth patterns. If FastMCP builds on these, use the provided
    patterns instead of rolling custom middleware.

    This eliminates the duplication entirely by extracting or unifying the
    two use cases.
  |||,
  properties=['no-oneoff-vars-and-trivial-wrappers'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/auth.py': [
      [75, 91],   // TokenAuthMiddleware extracts Bearer token
      [144, 161], // UITokenAuthMiddleware extracts Bearer token
    ],
  },
  gap_note= |||
    This finding represents a pattern that could be a property: "extract-duplicated-logic"
    or "DRY-across-similar-classes".

    When the same logic (token extraction, validation, parsing) appears in multiple
    classes or functions:
    - Extract to a shared helper function (preferred for simple cases)
    - Use framework-provided utilities if available
    - Consolidate into a single parameterized implementation if the classes are similar

    Related to but broader than "no-oneoff-vars-and-trivial-wrappers" - this is about
    identifying and extracting duplicated logic patterns across multiple contexts,
    not just within a single function.
  |||,
)
