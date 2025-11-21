local I = import '../../specimens/lib.libsonnet';

// iss-039: Agents infrastructure is overcomplicated, should use compositor pattern

I.issueOneOccurrence(
  rationale=|||
    The agents MCP bridge infrastructure uses a complex two-layer observer pattern with callbacks
    to bridge business logic classes to a monolithic AgentsServer. This architecture has multiple
    problems and is unnecessarily complicated.

    **Current Architecture (Complex):**

    ```
    Layer 1: Business Logic → MCP Server (via callbacks)
      ApprovalPolicyEngine ──[callback]──→ AgentsServer
      ApprovalHub ──[callback]──→ AgentsServer
      Session (UI state) ──[callback]──→ AgentsServer
      Session (session state) ──[callback]──→ AgentsServer
      Compositor ──[callback]──→ AgentsServer
      AgentRegistry ──[callback]──→ AgentsServer

    Layer 2: MCP Server → MCP Clients
      AgentsServer ──[ResourceUpdated]──→ subscribed clients
    ```

    **Problems with Current Architecture:**

    1. **Two-layer complexity:** Business logic doesn't directly broadcast MCP notifications.
       Instead, it calls sync callbacks that schedule async tasks that eventually broadcast.

    2. **Clobbered notifier bug (Issue 037):** `ApprovalPolicyEngine` needs to notify TWO servers
       (`ApprovalPolicyServer` and `AgentsServer`), but has only ONE notifier slot. When
       `agents.py:855` wires up its notifier, it replaces the one installed by
       `ApprovalPolicyServer.__init__()`, breaking notifications to that server.

    3. **Observer pattern bugs:** The 0-or-1 notifier pattern (single `Callable | None` instead of
       `list[Callable]`) prevents multiple consumers. See Issue 037 for full details.

    4. **Monolithic server:** `AgentsServer` aggregates resources from 6+ different business logic
       classes into one giant server. Hard to test, understand, and maintain.

    5. **Manual notification wiring:** Lines 833-932 in `agents.py` contain ~100 lines of
       duplicated factory functions (`make_policy_notifier`, `make_approval_hub_notifier`, etc.)
       that wire callbacks with fire-and-forget exception handling. See Issue 036.

    6. **URI scoping inconsistency (Issue 038):** Global approval policy URIs
       (`resource://approval-policy/*`) mixed with agent-scoped URIs
       (`resource://agents/{id}/*`).

    **Better Architecture (Compositor Pattern):**

    The agent-side infrastructure already uses the compositor pattern successfully. The agents
    bridge should use the same pattern:

    ```
    Small MCP Servers (wrap business logic directly) → Compositor → HTTP/SSE → MCP clients
                                                              ↓
                                              Notifications propagate automatically!
    ```

    **Concrete Design:**

    Replace monolithic `AgentsServer` with small focused servers mounted in a compositor:

    ```python
    # Small server wrapping ApprovalPolicyEngine
    class ApprovalPolicyServer(NotifyingFastMCP):
        def __init__(self, engine: ApprovalPolicyEngine, agent_id: AgentID):
            self._engine = engine
            self._agent_id = agent_id

        @self.resource(...)
        async def get_policy(self):
            return self._engine.get_policy()

        async def set_policy(self, source: str):
            result = await self._engine.set_policy_without_notify(source)
            # Direct broadcast to MCP clients - no callback layer
            await self.broadcast_resource_updated(
                f"resource://agents/{self._agent_id}/approval-policy/policy.py"
            )
            return result

    # Small server wrapping ApprovalHub
    class ApprovalsServer(NotifyingFastMCP):
        def __init__(self, hub: ApprovalHub, agent_id: AgentID):
            self._hub = hub
            self._agent_id = agent_id

        @self.resource(...)
        async def get_pending_approvals(self):
            return await self._hub.get_pending()

        async def approve_call(self, call_id: str):
            await self._hub.approve_without_notify(call_id)
            await self.broadcast_resource_updated(
                f"resource://agents/{self._agent_id}/approvals/pending"
            )
            await self.broadcast_resource_updated(
                f"resource://agents/{self._agent_id}/approvals/history"
            )

    # Similar small servers for:
    # - SessionStateServer (wraps Session)
    # - AgentRegistryServer (wraps AgentRegistry)
    # - etc.

    # Compose them:
    compositor = Compositor("agents")

    for agent_id in agent_ids:
        infra = await create_agent_infrastructure(agent_id)

        # Mount per-agent servers with namespacing
        await compositor.mount_inproc(
            f"agent_{agent_id}_policy",
            ApprovalPolicyServer(infra.approval_engine, agent_id)
        )
        await compositor.mount_inproc(
            f"agent_{agent_id}_approvals",
            ApprovalsServer(infra.approval_hub, agent_id)
        )
        await compositor.mount_inproc(
            f"agent_{agent_id}_session",
            SessionStateServer(infra.session, agent_id)
        )

    # Global registry server
    await compositor.mount_inproc("registry", AgentRegistryServer(registry))

    # Standard infrastructure (resources, compositor_meta, compositor_admin)
    await mount_standard_inproc_servers(compositor, gateway_client)

    # Expose over HTTP
    app = create_http_transport(compositor)
    ```

    **How Notifications Propagate:**

    The compositor automatically propagates notifications from mounted servers to clients:

    ```python
    # In ApprovalPolicyServer
    await self.broadcast_resource_updated("resource://agents/123/approval-policy/policy.py")
                    ↓
    # Compositor's _ChildHandler captures it (compositor/server.py:446-450)
    async def on_resource_updated(self, message):
        await self._compositor._notify_resource_updated(
            self._name,  # "agent_123_policy"
            str(message.params.uri)
        )
                    ↓
    # Flows to all compositor clients subscribed to that resource
    ```

    This is implemented in `compositor/server.py:446-469` and tested in
    `tests/mcp/resources/test_subscriptions_index.py`.

    **Benefits:**

    1. **Eliminates Layer 1:** No callback bridges. Servers directly broadcast to MCP clients.

    2. **Fixes clobbered notifier bug:** No shared notifier slots. Each server manages its own
       client subscriptions.

    3. **Fixes observer pattern bugs:** MCP servers natively support N subscribers via the MCP
       protocol. No need for custom observer implementation.

    4. **Natural namespacing:** Resources get server prefix
       (`agent_123_policy://resource://agents/123/approval-policy/policy.py`). Clear ownership.

    5. **Eliminates manual wiring:** No factory functions, no fire-and-forget tasks, no
       exception swallowing. Compositor handles everything.

    6. **Fixes URI scoping:** Per-agent servers naturally expose agent-scoped URIs
       (`resource://agents/{id}/...`). No global URIs.

    7. **Independently testable:** Each small server can be tested in isolation without mocking
       callbacks or worrying about notification wiring.

    8. **Reuses existing pattern:** Agent-side already uses compositor pattern successfully
       (see `agent/runtime/infrastructure.py`, `agent/cli.py`, `agent/matrix_bot.py`).

    9. **Dynamic mounting:** Can add/remove agent servers at runtime using compositor's
       `mount_server()`/`unmount_server()` API.

    10. **Standard tooling:** Get resources server, compositor_meta, compositor_admin for free
        via `mount_standard_inproc_servers()`.

    **What needs to be refactored:**

    - `agent/mcp_bridge/servers/agents.py` (833 lines) → Delete, replace with small servers
    - `agent/mcp_bridge/server.py` → Use compositor instead of single FastAPI app
    - Business logic classes (ApprovalPolicyEngine, ApprovalHub, etc.) → Remove notifier fields,
      add `*_without_notify()` methods that don't trigger notifications
    - Small server implementations → Create new files (approval_policy_server.py,
      approvals_server.py, session_server.py, registry_server.py)
    - Wiring code (lines 833-932 in agents.py) → Delete, replace with compositor mounting

    **Related Issues:**

    - Issue 036: Notification wiring has duplicated boilerplate
    - Issue 037: Notifier pattern has 5 design problems
    - Issue 038: Approval policy URIs should be agent-scoped

    All three issues are solved by using the compositor pattern.

    **Precedent:**

    The agent-side already uses this pattern:

    ```python
    # agent/runtime/infrastructure.py:100-114
    compositor = Compositor("compositor", eager_open=True)
    for name, server_cfg in mcp_config.mcpServers.items():
        await compositor.mount_server(name, server_cfg)

    # Mount approval policy servers
    await compositor.mount_inproc(APPROVAL_POLICY_SERVER_NAME_READER, reader_server)
    await compositor.mount_inproc(APPROVAL_POLICY_SERVER_NAME_PROPOSER, proposer_server)

    # Standard infrastructure
    await mount_standard_inproc_servers(compositor, gateway_client)
    ```

    The agents bridge should follow the same proven pattern.
  |||,
  properties=['architectural-design', 'complexity', 'compositor-pattern', 'notifications'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [1, 932],  // Entire monolithic AgentsServer file
      [833, 932],  // Notification wiring code (Issue 036)
    ],
    'adgn/src/adgn/agent/mcp_bridge/server.py': [
      [1, 500],  // InfrastructureRegistry and HTTP bridge setup
    ],
    'adgn/src/adgn/agent/approvals.py': [
      [96, 110],   // ApprovalHub with _notifier field
      [146, 195],  // ApprovalPolicyEngine with _notify field
    ],
  },
)
