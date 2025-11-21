local I = import '../../specimens/lib.libsonnet';

// iss-037: Notifier pattern is brittle and has multiple design problems

I.issueOneOccurrence(
  rationale=|||
    The "notifier" callback pattern used throughout the codebase (ApprovalHub, ApprovalPolicyEngine,
    AgentRegistry, sessions) has several design problems that make it brittle and error-prone:

    **Problem 1: 0-or-1 receivers, not N**

    Each class has a single notifier field (`_notifier`, `_notify`) that gets replaced with `set_notifier()`.
    Only one listener can be registered at a time - not a proper observer/pub-sub pattern.

    **Examples:**
    - `ApprovalHub._notifier: Callable[[], None] | None` (line 82)
    - `ApprovalPolicyEngine._notify: Callable[[str], None] | None` (line 156)
    - `AgentRegistry._notifier: Callable[[str], Awaitable[None]] | None` (server.py:92)

    If you need multiple consumers, you must manually create a wrapper function that calls all of them:

    ```python
    # Current pattern forces this workaround:
    def multi_notifier():
        notifier1()
        notifier2()
        notifier3()
    hub.set_notifier(multi_notifier)
    ```

    **Problem 2: Mixed sync/async with awkward contract**

    Notifiers are typed as sync callables but are documented as "sync and non-blocking (may schedule async work)":

    **ApprovalHub contract (approvals.py:87):**
    ```python
    def set_notifier(self, notifier: Callable[[], None]) -> None:
        """Install/replace the out-of-band notifier for approval state changes.

        Contract: notifier() is sync and non-blocking (may schedule async work).
        """
    ```

    **ApprovalPolicyEngine contract (approvals.py:165):**
    ```python
    def set_notifier(self, notifier: Callable[[str], None]) -> None:
        """Install/replace the out-of-band notifier for resource changes.

        Contract: notifier(uri) is sync and non-blocking (may schedule async work).
        """
    ```

    But the AgentRegistry expects async:
    ```python
    def set_notifier(self, notifier: Callable[[str], Awaitable[None]]) -> None:
    ```

    This inconsistency is confusing. The "sync but schedules async work" pattern forces implementations
    to use `loop.create_task()`:

    **approval_policy/server.py:96-100:**
    ```python
    def _notify(uri: str) -> None:
        # Fire-and-forget; schedule broadcast and signal completion to waiters
        logger.debug("engine notify uri=%s", uri)
        task = asyncio.create_task(self._broadcast_and_signal(uri))
        task.add_done_callback(lambda t: t.exception() if t.done() and not t.cancelled() else None)
    ```

    **Problem 3: Exception swallowing**

    When notifiers are called directly (sync), exceptions propagate:
    ```python
    if self._notifier:
        self._notifier()  # Exception propagates up
    ```

    But when notifiers use `create_task()` (fire-and-forget), exceptions are swallowed or only logged:

    **agents.py:844-851:**
    ```python
    loop = asyncio.get_running_loop()
    _task = loop.create_task(server.broadcast_resource_updated(uri))
    # Don't await task - fire and forget notification
    _task.add_done_callback(
        lambda t: logger.debug(f"Broadcast complete for {uri}")
        if not t.exception()
        else logger.warning(f"Broadcast failed for {uri}: {t.exception()}")
    )
    ```

    The exception is logged but not re-raised. If `broadcast_resource_updated` fails, the caller never knows.

    **approval_policy/server.py:100:**
    ```python
    task.add_done_callback(lambda t: t.exception() if t.done() and not t.cancelled() else None)
    ```

    This accesses `t.exception()` only to prevent asyncio warnings - doesn't actually handle or log the error!

    **Problem 4: No exception handling at call sites**

    Notifiers are called without try/except:

    **approvals.py:101-102:**
    ```python
    if self._notifier:
        self._notifier()  # No try/except - exception crashes the caller
    return await fut
    ```

    **approvals.py:109-110:**
    ```python
    if self._notifier:
        self._notifier()  # No try/except
    ```

    **approvals.py:178-181:**
    ```python
    if self._notify:
        self._notify(APPROVAL_POLICY_RESOURCE_URI)  # No try/except
        # Also notify agent-specific policy state resource
        self._notify(AGENTS_POLICY_STATE_URI_FMT.format(agent_id=self.agent_id))  # No try/except
    ```

    If a notifier throws, it crashes the whole operation (e.g., policy update fails).

    **Problem 5: Inconsistent patterns**

    Some places guard with `if self._notifier:`, others use intermediate `cb` variable:

    **Pattern 1 - Direct check (lines 101, 109, 178):**
    ```python
    if self._notifier:
        self._notifier()
    ```

    **Pattern 2 - Intermediate variable (lines 204-206, 209-211):**
    ```python
    cb = self._notify
    if cb:
        cb(uri)
    ```

    The intermediate variable pattern is pointless - just adds a line for no benefit.

    **What should be done:**

    **Option 1: Use proper async observer pattern with URI parameters**

    Replace single notifier with a list of async observers that receive URI parameters for granular notifications:

    ```python
    class ApprovalHub:
        def __init__(self):
            self._observers: list[Callable[[str], Awaitable[None]]] = []

        def add_observer(self, observer: Callable[[str], Awaitable[None]]) -> None:
            self._observers.append(observer)

        def remove_observer(self, observer: Callable[[str], Awaitable[None]]) -> None:
            self._observers.remove(observer)

        async def _notify_observers(self, uri: str) -> None:
            for observer in self._observers:
                try:
                    await observer(uri)
                except Exception as e:
                    logger.warning(f"Observer notification failed for {uri}: {e}", exc_info=True)
                    # Continue notifying other observers
    ```

    Then call sites become:
    ```python
    await self._notify_observers(resources.agent_approvals_pending(self.agent_id))
    ```

    **Benefits:**
    - Multiple observers supported natively
    - Observers receive specific URI that changed (granular notifications)
    - Consistent async/await pattern (no sync/async mixing)
    - Explicit exception handling per observer
    - Failed observers don't prevent other observers from being notified
    - Type-safe (no "sync but may schedule async" hack)

    **Option 2: Use asyncio events/queues instead of callbacks**

    Replace callbacks with structured events that include URI information:

    ```python
    @dataclass
    class ResourceChangeEvent:
        uri: str
        timestamp: datetime

    class ApprovalHub:
        def __init__(self):
            self._event_queue: asyncio.Queue[ResourceChangeEvent] = asyncio.Queue()

        async def await_decision(...):
            # ... set up pending ...
            await self._event_queue.put(
                ResourceChangeEvent(uri=resources.agent_approvals_pending(self.agent_id),
                                   timestamp=datetime.now())
            )
            return await fut

        async def notification_listener(self):
            while True:
                event = await self._event_queue.get()
                # Handle event (broadcast event.uri, etc.)
    ```

    **Benefits:**
    - Decouples event producers from consumers
    - Events are queued and processed in order
    - Can include additional metadata (timestamp, event type, etc.)
    - Natural backpressure via queue size limits
    - Easier to test (can drain queue and inspect events)

    **Recommendation:**
    Option 1 (async observer pattern with URI parameters) is simpler and more direct. It requires
    minimal refactoring and naturally fits the current call-site patterns. Option 2 (event queues)
    is more complex but provides better decoupling if that becomes necessary.

    **Impact:**
    This is a fundamental architectural issue that affects:
    - `ApprovalHub` (2 notifier call sites)
    - `ApprovalPolicyEngine` (5+ notifier call sites)
    - `AgentRegistry` (1 notifier call site)
    - Session notifiers (UI state, session state)
    - All the wiring code in agents.py (lines 833-932)
  |||,
  properties=['architectural-design', 'error-handling', 'async-patterns'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [82, 82],    // ApprovalHub._notifier field
      [84, 89],    // set_notifier with "sync and non-blocking" contract
      [101, 102],  // Unguarded notifier call in await_decision
      [109, 110],  // Unguarded notifier call in resolve
      [156, 156],  // ApprovalPolicyEngine._notify field
      [162, 167],  // set_notifier with "sync and non-blocking" contract
      [178, 181],  // Unguarded notify calls
      [204, 206],  // Unnecessary intermediate cb variable pattern
      [209, 211],  // Unnecessary intermediate cb variable pattern
    ],
    'adgn/src/adgn/mcp/approval_policy/server.py': [
      [96, 100],   // Fire-and-forget notifier with exception swallowing
    ],
    'adgn/src/adgn/agent/mcp_bridge/server.py': [
      [87, 92],    // AgentRegistry.set_notifier (async variant)
      [182, 183],  // Unguarded notifier call
    ],
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [844, 851],  // Fire-and-forget pattern with logged exceptions
      [870, 874],  // Fire-and-forget pattern with logged exceptions
      [890, 894],  // Fire-and-forget pattern with logged exceptions
      [907, 911],  // Fire-and-forget pattern with logged exceptions
    ],
  },
)
