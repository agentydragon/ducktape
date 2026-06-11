# Class Constants Necessity Analysis

> **Status:** Research complete. Conclusion: class constants ARE needed alongside instance attributes.

## Question

Can we eliminate tool name class constants by refactoring contexts to have server instances?

## Answer: No -- class constants are required in most contexts.

The pattern of "steps constructed before servers exist" is pervasive. Server instances are available ONLY during agent execution, not during test/bootstrap construction.

### Where server instances are NOT available

1. **Test fixtures** -- steps are constructed before entering compositor context managers. `EchoCall("test")` is built before any server exists. Fixture hierarchy yields only a `Client`, not server instances.

2. **Policy evaluation** -- policies run in sandboxed Docker containers. They import constants from `_shared/constants.py` (mount prefixes) and server modules (tool names). No live server instances.

3. **Prompt templates** -- Mako/Jinja templates reference tool names as strings. Cannot call instance methods.

### Where server instances ARE available

- **Bootstrap helpers** (`compositor_helpers.py`) -- `_mount_standard_servers()` has access to freshly created server instances. These should use instance attributes instead of string literals.

## Recommendation (Priority Order)

1. **HIGH: Bootstrap helpers** -- replace string literals with server instance attributes (easy fix)
2. **HIGH: Test fixtures** -- expose server instances from compositor context managers, update step constructors
3. **MEDIUM: Dual-access pattern** -- complete typed server subclasses with both class constants and instance attributes
4. **LOW: `WellKnownTools` enum** -- redundant with server class constants, eliminate (only 3 entries, test-only usage)
5. **LOW: Audit unused constants** -- identify dead constants from runtime, resources, chat, loop servers

## Note on `WellKnownTools`

`x/agent_server/approvals.py` defines a `WellKnownTools` StrEnum that duplicates constants already on server classes (`UiServer.SEND_MESSAGE_TOOL_NAME`, etc.). Policies CAN import from server classes directly. This enum should be eliminated, but it's low priority (3 entries, test-only usage).
