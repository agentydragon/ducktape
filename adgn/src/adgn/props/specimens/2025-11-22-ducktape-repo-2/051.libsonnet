local I = import '../../specimens/lib.libsonnet';

// iss-051: Frontend tests reference non-existent /ws/approvals WebSocket endpoint

I.issueOneOccurrence(
  rationale=|||
    The ApprovalTimeline tests reference a WebSocket endpoint `/ws/approvals` that doesn't
    exist in the backend:

    ```typescript
    const wsUrl = `ws://localhost/ws/approvals?agent_id=${encodeURIComponent(mockAgentId)}`
    const ws = new MockWebSocket(wsUrl)
    ```

    This appears in multiple test cases (lines 486, 512, 538, 601).

    However, the backend doesn't have this endpoint. In app.py lines 320-321:

    ```python
    # TODO: Register websocket routes (placeholder)
    # register_agents_ws(app)
    ```

    The WebSocket routes are commented out and never registered!

    Additionally, comments in stores_channels.ts indicate this endpoint was supposed to
    be replaced by MCP resources:

    ```typescript
    // - resource://agents/{agentId}/approvals/pending - pending approvals (replaces /ws/approvals)
    ```

    and

    ```typescript
    // Subscribe to approvals via MCP resource (replaces /ws/approvals)
    ```

    and

    ```typescript
    // Replaces WebSocket /ws/approvals channel with resource://agents/{agentId}/approvals/pending
    ```

    So the tests are testing against:
    1. An endpoint that doesn't exist in the backend
    2. An endpoint that was intentionally replaced by MCP resources

    Fix:
    1. Update tests to use MCP resources instead of WebSocket endpoints
    2. Remove all references to the old /ws/approvals endpoint
    3. If WebSocket support is still needed, either:
       - Implement the backend endpoint (uncomment register_agents_ws), or
       - Remove the TODO comment if WebSocket is no longer planned

    This is a case of dead test code testing against a non-existent/deprecated API.
  |||,
  properties=['no-dead-code', 'truthfulness'],
  filesToRanges={
    'adgn/src/adgn/agent/web/src/components/ApprovalTimeline.test.ts': [
      486,  // ws://localhost/ws/approvals reference
      512,  // ws://localhost/ws/approvals reference
      538,  // ws://localhost/ws/approvals reference
      601,  // ws://localhost/ws/approvals reference
    ],
    'adgn/src/adgn/agent/server/app.py': [
      [320, 321],  // TODO comment about WebSocket routes never registered
    ],
  },
  gap_note=|||
    This pattern deserves a property like "no-tests-for-nonexistent-apis": when tests
    reference API endpoints (HTTP, WebSocket, etc.) that don't exist in the implementation,
    either the tests are wrong or the implementation is incomplete. This is more specific
    than "no-dead-code" as it's about test-implementation mismatch, and more specific than
    "truthfulness" as it's specifically about tests claiming to test functionality that
    doesn't exist. It's related to test quality and API contract verification.
  |||,
)
