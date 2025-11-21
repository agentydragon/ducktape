local I = import '../../specimens/lib.libsonnet';

// iss-036: Wired-up notifiers have duplicated common structure that should be deduplicated

I.issueOneOccurrence(
  rationale=|||
    The notification wiring code (lines 833-932) contains 4 notifier factory functions that follow
    the exact same pattern with duplicated boilerplate. This common structure should be extracted
    into a helper function.

    **Duplicated pattern in 4 notifiers:**

    1. **make_policy_notifier** (lines 841-855)
    2. **make_ui_state_notifier** (lines 884-898)
    3. **make_session_state_notifier** (lines 901-915)
    4. **make_approval_hub_notifier** (lines 858-878) - same pattern but broadcasts multiple URIs

    **Common structure repeated in each:**
    ```python
    def make_X_notifier(aid: str):
        def notifier(...):
            # Notifier is sync, schedule broadcast in event loop
            loop = asyncio.get_running_loop()
            _task = loop.create_task(server.broadcast_resource_updated(uri))
            # Don't await task - fire and forget notification
            _task.add_done_callback(
                lambda t: logger.debug(f"Broadcast complete for {uri}")
                if not t.exception()
                else logger.warning(f"Broadcast failed for {uri}: {t.exception()}")
            )
        return notifier
    ```

    **Specific examples:**

    **make_policy_notifier (lines 841-855):**
    ```python
    def make_policy_notifier(aid: str):
        def notifier(uri: str):
            # Notifier is sync, schedule broadcast in event loop
            loop = asyncio.get_running_loop()
            _task = loop.create_task(server.broadcast_resource_updated(uri))
            # Don't await task - fire and forget notification
            _task.add_done_callback(
                lambda t: logger.debug(f"Broadcast complete for {uri}")
                if not t.exception()
                else logger.warning(f"Broadcast failed for {uri}: {t.exception()}")
            )
        return notifier
    ```

    **make_ui_state_notifier (lines 884-898):**
    ```python
    def make_ui_state_notifier(aid: AgentID):
        def notifier():
            # Notifier is sync, schedule broadcast in event loop
            loop = asyncio.get_running_loop()
            _task = loop.create_task(server.broadcast_resource_updated(resources.agent_ui_state(aid)))
            # Don't await task - fire and forget notification
            _task.add_done_callback(
                lambda t: logger.debug(f"UI state broadcast complete for {aid}")
                if not t.exception()
                else logger.warning(f"UI state broadcast failed for {aid}: {t.exception()}")
            )
        return notifier
    ```

    **make_session_state_notifier (lines 901-915):**
    ```python
    def make_session_state_notifier(aid: AgentID):
        def notifier():
            # Notifier is sync, schedule broadcast in event loop
            loop = asyncio.get_running_loop()
            _task = loop.create_task(server.broadcast_resource_updated(resources.agent_session_state(aid)))
            # Don't await task - fire and forget notification
            _task.add_done_callback(
                lambda t: logger.debug(f"Session state broadcast complete for {aid}")
                if not t.exception()
                else logger.warning(f"Session state broadcast failed for {aid}: {t.exception()}")
            )
        return notifier
    ```

    **make_approval_hub_notifier (lines 858-878) - broadcasts multiple URIs:**
    ```python
    def make_approval_hub_notifier(aid: AgentID):
        def notifier():
            # Notifier is sync, schedule broadcast in event loop
            loop = asyncio.get_running_loop()
            # Broadcast all relevant approval resources
            uris = [
                resources.agent_approvals_pending(aid),
                resources.agent_approvals_history(aid),
                resources.APPROVALS_PENDING_GLOBAL,
            ]
            for uri in uris:
                _task = loop.create_task(server.broadcast_resource_updated(uri))
                _task.add_done_callback(
                    lambda t, u=uri: logger.debug(f"Broadcast complete for {u}")
                    if not t.exception()
                    else logger.warning(f"Broadcast failed for {u}: {t.exception()}")
                )
        return notifier
    ```

    **Why this is problematic:**

    1. **Massive duplication**: The same 10-15 line pattern is repeated 4 times with only minor
       variations (URI source, log message details).

    2. **Hard to maintain**: If the broadcast pattern needs to change (e.g., error handling,
       logging format, task cleanup), must update 4 identical copies.

    3. **Error-prone**: Easy to update one notifier but forget the others, leading to inconsistencies.

    4. **Verbose**: ~60 lines of code for what should be ~15 lines + 4 simple calls.

    5. **Same callback pattern**: The `add_done_callback` lambda is identical in structure across
       all notifiers, just with different log messages.

    **Recommended fix:**

    Create a helper function that handles the common pattern:

    ```python
    def make_sync_broadcast_notifier(
        *,
        uri_getter: Callable[[], str | list[str]],
        log_context: str
    ) -> Callable[[], None]:
        """Create a sync notifier that broadcasts resource updates.

        Args:
            uri_getter: Function that returns URI or list of URIs to broadcast
            log_context: Context string for log messages (e.g., "policy", "UI state")

        Returns:
            Sync notifier function that schedules broadcasts as fire-and-forget tasks
        """
        def notifier():
            loop = asyncio.get_running_loop()
            uris = uri_getter()
            if isinstance(uris, str):
                uris = [uris]

            for uri in uris:
                _task = loop.create_task(server.broadcast_resource_updated(uri))
                _task.add_done_callback(
                    lambda t, u=uri: logger.debug(f"{log_context} broadcast complete for {u}")
                    if not t.exception()
                    else logger.warning(f"{log_context} broadcast failed for {u}: {t.exception()}")
                )

        return notifier
    ```

    **Usage:**

    ```python
    # Before (lines 841-855):
    def make_policy_notifier(aid: str):
        def notifier(uri: str):
            loop = asyncio.get_running_loop()
            _task = loop.create_task(server.broadcast_resource_updated(uri))
            _task.add_done_callback(...)
        return notifier
    infra.approval_engine.set_notifier(make_policy_notifier(agent_id))

    # After:
    # For policy notifier (takes URI as parameter)
    def make_policy_notifier(aid: str):
        def notifier(uri: str):
            return make_sync_broadcast_notifier(
                uri_getter=lambda: uri,
                log_context="Policy"
            )()
        return notifier
    infra.approval_engine.set_notifier(make_policy_notifier(agent_id))

    # For UI state notifier (no parameters)
    infra.session.set_ui_state_notifier(
        make_sync_broadcast_notifier(
            uri_getter=lambda: resources.agent_ui_state(agent_id),
            log_context="UI state"
        )
    )

    # For session state notifier (no parameters)
    infra.session._manager.set_session_state_notifier(
        make_sync_broadcast_notifier(
            uri_getter=lambda: resources.agent_session_state(agent_id),
            log_context="Session state"
        )
    )

    # For approval hub notifier (multiple URIs)
    infra.approval_hub.set_notifier(
        make_sync_broadcast_notifier(
            uri_getter=lambda: [
                resources.agent_approvals_pending(agent_id),
                resources.agent_approvals_history(agent_id),
                resources.APPROVALS_PENDING_GLOBAL,
            ],
            log_context="Approval hub"
        )
    )
    ```

    **Benefits:**
    - Single source of truth for broadcast pattern (~15 lines instead of ~60)
    - Changes to broadcast logic happen in one place
    - More concise usage code
    - Harder to make mistakes (can't forget to update one copy)
    - Clearer intent (helper name documents the pattern)

    **Note:**
    `make_mount_listener` (lines 918-926) is different - it's async and awaits the broadcast,
    so it shouldn't be included in this refactoring. The registry_notifier (lines 929-930) is
    also different (already async and simple).
  |||,
  properties=['duplication', 'dry-principle', 'maintainability'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [833, 932],  // All notification wiring code
      [841, 855],  // make_policy_notifier
      [858, 878],  // make_approval_hub_notifier
      [884, 898],  // make_ui_state_notifier
      [901, 915],  // make_session_state_notifier
    ],
  },
)
