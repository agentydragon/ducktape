# Class Constants Necessity Analysis

## Executive Summary

**Research question:** Can we eliminate tool name class constants by refactoring contexts to have server instances, or do we genuinely need them?

**Answer:** **Yes, we need class constants in most contexts.** The dual-access pattern is justified.

**Key finding:** The pattern of "steps constructed before servers exist" is pervasive across the codebase. Server instances are available ONLY during agent execution, not during test/bootstrap construction time.

---

## 1. Test Fixtures - Server Instances NOT Available

### Current Architecture

**Fixture hierarchy** (from `tests/conftest.py`):

```python
# Line 314-335: Factory fixtures return context managers
@pytest.fixture
def make_pg_client(sqlite_persistence, docker_client, test_agent_id):
    @asynccontextmanager
    async def _open(servers: McpServerSpecs, *, policy_engine: PolicyEngine | None = None):
        # ... creates compositor, mounts servers
        async with Compositor("comp") as comp:
            await _setup_mounted_compositor(comp, servers, policy_engine)
            async with Client(comp) as sess:
                yield sess  # Only yields CLIENT, not servers
    return _open
```

**Test usage** (from `tests/agent/test_approval_integration.py:31`):

```python
# Steps are constructed BEFORE entering the compositor context
mock = make_step_runner(steps=[EchoCall("test"), AssistantMessage("done")])
client = make_mock(mock.handle_request_async)

# Servers only exist INSIDE this async context
async with make_pg_compositor(servers, policy_engine=engine) as (mcp_client, policy_engine):
    agent = await Agent.create(...)  # agent.run() will execute steps
```

**Steps use constants** (from `tests/support/steps.py:18,203-204`):

```python
from mcp_infra.testing.simple_servers import ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME, EchoInput

@dataclass
class EchoCall:
    text: str

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult:
        return factory.make_mcp_tool_call(ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME, EchoInput(text=self.text))
```

### Why This Pattern Exists

**Lifecycle separation:**

1. **Test function body:** Construct step sequences (declarative test scenario)
2. **Async context entry:** Create compositor, mount servers
3. **Agent.run():** Execute steps against live servers

**Constants are necessary because:**

- Steps are Python objects constructed in synchronous test code
- Server instances don't exist yet (they're created inside async context managers)
- Steps need to reference tool names NOW, but servers only exist LATER

### Could We Refactor?

**Option A: Pass server instances to step constructors**

```python
# BROKEN: servers don't exist yet!
async with make_pg_compositor({"echo": echo_server}) as (client, engine):
    # NOW we have servers, but steps were constructed earlier:
    steps = [EchoCall("test", server=echo_server)]  # Too late!
```

**Option B: Make steps factories/lambdas**

```python
# Possible but verbose and loses declarative clarity
steps = [
    lambda servers: EchoCall(servers["echo"].echo_tool.name, "test"),
    lambda servers: AssistantMessage("done")
]
```

**Option C: Extract tool names from fixtures after mounting**

```python
# Possible but adds complexity - need to thread servers through fixtures
@pytest.fixture
async def echo_with_metadata(make_simple_mcp):
    server = make_simple_mcp
    # Return both server AND extracted metadata
    return server, {"tool_names": {"echo": server.echo_tool.name}}

async def test_foo(echo_with_metadata):
    server, metadata = echo_with_metadata
    # Now construct steps using metadata...
    steps = [EchoCall(metadata["tool_names"]["echo"], "test")]
```

### Recommendation

**Keep class constants for test steps.** The declarative pattern is valuable:

- Test scenarios are readable: `[EchoCall("test"), AssistantMessage("done")]`
- Construction happens before server lifecycle
- Constants are compile-time safe (typos caught by IDE/mypy)

**Refactoring difficulty:** Hard (pervasive pattern, ~50+ test files affected)

**Value of refactoring:** Low (would make tests more complex, not simpler)

---

## 2. Bootstrap Handlers - Server Instances ARE Available! ✅

### Current Usage

**Initialization order** (from `adgn/gitea_pr_gate/agent_backend.py:218-232`):

```python
async with Compositor() as comp:
    # 1. Servers are created and mounted FIRST
    git_server = await attach_git_ro(comp, repo_root)
    await comp.mount_inproc(SUBMIT_COMMIT_MESSAGE_MOUNT_PREFIX, make_submit_server(...))

    # 2. Bootstrap construction happens AFTER servers exist
    builder = TypedBootstrapBuilder.for_server(git_server)  # Has server instance!
    bootstrap_calls = make_commit_bootstrap_calls(builder, "git_ro", amend=amend)
    bootstrap = SequenceHandler([InjectItems(items=bootstrap_calls)])

    # 3. Agent creation happens last
    agent = await Agent.create(..., handlers=[bootstrap])
```

**Helper pattern using string literals** (from `adgn/gitea_pr_gate/agent_backend.py:56-57`):

```python
def make_commit_bootstrap_calls(
    builder: TypedBootstrapBuilder,
    server: str,  # Mount prefix
    ...
) -> list[FunctionCallItem]:
    calls = [
        builder.call(server, "git_status", StatusInput(...)),  # ← String literal!
        builder.call(server, "git_diff", DiffInput(...)),      # ← String literal!
    ]
```

### Analysis

**KEY FINDING: Servers are mounted BEFORE bootstrap construction!**

The initialization order is:

1. Create compositor
2. Create and mount servers
3. Construct bootstrap calls (servers exist here!)
4. Create agent with bootstrap handler

**This means bootstrap helpers CAN receive server instances:**

### Can We Use Server Instances? YES

**Current state (string literals - DoD violation):**

```python
# From adgn/gitea_pr_gate/agent_backend.py:54-68
def make_commit_bootstrap_calls(
    builder: TypedBootstrapBuilder,
    server: str,
    amend: bool = False,
) -> list[ToolCall]:
    return [
        builder.call(server, "git_status", StatusInput(...)),  # ← String literal!
        builder.call(server, "git_diff", DiffInput(...)),      # ← String literal!
        builder.call(server, "git_log", LogInput(...)),        # ← String literal!
    ]
```

**Proposed refactoring (pass server instance):**

```python
def make_commit_bootstrap_calls(
    builder: TypedBootstrapBuilder,
    mount_prefix: str,
    git_server: GitRoServer,  # ← Accept server instance
    amend: bool = False,
) -> list[ToolCall]:
    return [
        builder.call(mount_prefix, git_server.git_status_tool.name, StatusInput(...)),
        builder.call(mount_prefix, git_server.git_diff_tool.name, DiffInput(...)),
        builder.call(mount_prefix, git_server.git_log_tool.name, LogInput(...)),
    ]

# Call site (agent_backend.py:224):
bootstrap_calls = make_commit_bootstrap_calls(builder, GIT_RO_SERVER_NAME, git_server, amend=amend)
```

**Benefits:**

- No string literals (satisfies DoD)
- Type-safe tool access (`server.tool.name`)
- Eliminates fragile introspection (current helpers use runtime tool discovery)
- Consistent with production code pattern

**Current fragile introspection** (from `agent_core/src/agent_core/bootstrap.py`):

```python
# Current helpers introspect at runtime (can fail!)
models = introspect_server_models(exec_server)
exec_tools = [name for name, (input_type, _) in models.items()
              if input_type and issubclass(input_type, ExecInput)]
if not exec_tools:
    raise RuntimeError("No exec tool found")
if len(exec_tools) > 1:
    raise RuntimeError(f"Multiple exec tools found: {exec_tools}")
exec_tool_name = exec_tools[0]  # Hope we got the right one!
```

### Recommendation

**Refactor bootstrap to use server instances!** This eliminates the need for class constants in bootstrap:

- Server instances ARE available at bootstrap construction time
- Can use typed tool attributes: `server.tool.name`
- More reliable than current introspection pattern
- Cleaner API for bootstrap helper functions

**Refactoring difficulty:** Easy (few bootstrap call sites, ~5 helper functions)

**Value of refactoring:** High (eliminates string literals, removes fragile introspection, improves type safety)

**No class constants needed for bootstrap!** ✅

---

## 3. Prompt Templates - Server Instances ARE Available! ✅

### Current Usage

**Templates rendered INSIDE async functions** (from `props/core/critic/critic.py:456-468`):

```python
async def _build_critic_instructions() -> str:
    """Build critic system instructions by rendering template."""
    # Compositor and servers already mounted at this point!
    compositor_instructions = comp.render_agent_dynamic_instructions()
    return render_prompt_template(  # ← Called AFTER servers mounted
        "critic/prompts/critic_system.j2.md",
        compositor_instructions=compositor_instructions,
        optimized_prompt=optimized_prompt,
    )

# Used as dynamic_instructions callback in agent (line ~470)
agent = await Agent.create(..., dynamic_instructions=_build_critic_instructions)
```

**Template rendering helper** (from `props/core/prompts/util.py:64-67`):

```python
def render_prompt_template(name: str, **ctx: object) -> str:
    env = get_templates_env()
    tmpl = env.get_template(name)
    return str(tmpl.render(**ctx)).strip()  # Accepts any context vars
```

### Analysis

**KEY FINDING: Templates are rendered AFTER servers are mounted!**

The rendering flow is:

1. Create compositor
2. Mount servers
3. Define `async def _build_critic_instructions()` that renders template
4. Create agent with `dynamic_instructions=_build_critic_instructions`
5. Agent calls the function to render instructions

**This means templates CAN receive server instances in render context:**

### Could We Use Server Instances?

**YES! Pass servers to template context:**

```python
# Current (no servers passed):
return render_prompt_template(
    "critic/prompts/critic_system.j2.md",
    compositor_instructions=compositor_instructions,
    optimized_prompt=optimized_prompt,
)

# Refactored (pass server instances):
return render_prompt_template(
    "critic/prompts/critic_system.j2.md",
    compositor_instructions=compositor_instructions,
    optimized_prompt=optimized_prompt,
    runtime_server=runtime_server,  # ← Pass server instance!
    critic_server=critic_server,     # ← Pass server instance!
)

# Template can use: {{ runtime_server.exec_tool.name }}
```

**Benefits:**

- No string literals in templates
- Type-safe tool access from server instances
- Consistent with bootstrap and production code patterns

**Exception: Module-level template rendering**

Some templates ARE rendered at module import (e.g., `engine.py:384` for policy instructions). These still need constants. But agent prompts rendered in async functions can use server instances.

### Recommendation

**Refactor agent prompt templates to use server instances!** Most templates are rendered after servers are mounted:

- Pass server instances in render context
- Use `{{ server.tool.name }}` in templates
- Eliminates string literals from agent prompts

**Keep constants for:** Module-level template rendering (rare, mainly policy instructions)

**Refactoring difficulty:** Easy (change render_prompt_template call sites to pass servers)

**Value of refactoring:** High (eliminates string literals from agent prompts, consistent with other patterns)

---

## 4. Step Classes - Could Be Refactored! ✅

### Current Construction Timing

**Current pattern** (from `tests/agent/test_approval_integration.py:30-36`):

```python
# Steps constructed FIRST (test body, before compositor context)
mock = make_step_runner(steps=[EchoCall("test"), AssistantMessage("done")])
client = make_mock(mock.handle_request_async)

# Servers created LATER (inside async context)
async with make_pg_compositor(servers, policy_engine=engine) as (mcp_client, _):
    agent = await Agent.create(mcp_client=mcp_client, client=client, ...)
```

### Analysis

**Is there a technical constraint preventing reordering?**

Let me check:

1. ✅ Test function is async (can await compositor first)
2. ✅ `make_step_runner` is just a factory (no special timing requirement)
3. ✅ Steps are dataclasses (no initialization dependencies)
4. ✅ Agent doesn't care when steps were created

**Answer: NO fundamental constraint!** It's just current implementation.

### Could We Refactor to Use Server Instances?

**YES! Just reorder the code:**

```python
# Refactored: Servers FIRST, then steps
async with make_pg_compositor(servers, policy_engine=engine) as (mcp_client, compositor):
    # Fixtures would expose server instances (new capability)
    echo_server = compositor.get_inproc_server("echo")

    # Construct steps with server instances
    mock = make_step_runner(steps=[
        EchoCall(echo_server, "test"),  # Pass server instance!
        AssistantMessage("done")
    ])
    client = make_mock(mock.handle_request_async)

    agent = await Agent.create(mcp_client=mcp_client, client=client, ...)
```

**Step constructor would change:**

```python
@dataclass
class EchoCall:
    echo_server: EchoServer  # Accept server instance
    text: str

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult:
        return factory.make_mcp_tool_call(
            "echo",  # Or from recipe
            self.echo_server.echo_tool.name,  # From instance!
            EchoInput(text=self.text)
        )
```

**Benefits:**

- No class constants needed
- Type-safe tool access from server instances
- Still declarative and readable
- Consistent with bootstrap/templates/production patterns

**Required changes:**

1. Enhance fixtures to expose server instances (easy - add to context manager return)
2. Update step constructors to accept server instances (easy - add parameter)
3. Update ~50+ test files to reorder construction (tedious but straightforward)

### Recommendation

**Tests CAN eliminate class constants!** It's refactoring work, not an architectural limitation.

**Refactoring difficulty:** Medium (pervasive but mechanical - ~50 test files)

**Value of refactoring:** High (eliminates last usage of class constants besides policy eval, achieves full consistency)

---

## 5. Policy Evaluation - DEFINITELY Needs Constants

### Why Policy Eval Is Special

**User confirmed:** Policy evaluation runs in Docker, constructs tool patterns to match against. Cannot easily get server instances.

**From `mcp_infra/approval_policy/instructions.j2.md:28-64`:**

```markdown
Read the current approval policy from: {{ TRUSTED_POLICY_URL }}

Input JSON (stdin):
{"name": "<server>\_<tool>", "arguments": {...}}

Minimal example:
from agent_server.policies.policy_types import ApprovalDecision, PolicyRequest, PolicyResponse
from mcp_infra.naming import tool_matches

req = PolicyRequest.model_validate_json(sys.stdin.read())
if tool_matches(req.name, server="resources", tool="read"):
decision = ApprovalDecision.ALLOW
```

**Policy programs use string matching:**

- Must match `"<server>_<tool>"` format
- No access to server instances (runs in isolated container)
- Needs constants for reliable matching

### Example Policy

```python
# Policy needs to match against tool names
if tool_matches(req.name, server="runtime", tool="exec"):
    # Check if command is safe...
    if cmd[0] in ALLOWED_COMMANDS:
        return PolicyResponse(decision=ApprovalDecision.ALLOW, ...)
```

**Cannot use server instances:**

- Policy runs in ephemeral Docker container
- No MCP connection to live servers
- Only has tool name string from request

### Recommendation

**Policy evaluation MUST use class constants.** This is non-negotiable:

- Isolated execution environment
- String-based tool matching
- No server instances available

**Refactoring difficulty:** Impossible (architectural constraint)

### Actual Constants Usage Audit

**Investigation question:** Which class constants does policy eval actually use?

**Two sources to check:**

1. Policy engine itself (`agent_server/src/agent_server/mcp/approval_policy/engine.py`)
2. **Default/packaged policies** that ship with the system (`agent_server/src/agent_server/policies/`)

#### Engine Constants Usage

**Template rendering context** (lines 395-401):

```python
def _load_instructions() -> str:
    raw = resources.files(__package__).joinpath("instructions.j2.md").read_text(encoding="utf-8")
    tmpl = Template(raw)
    rendered = tmpl.render(
        RUNTIME_MOUNT_PREFIX=RUNTIME_MOUNT_PREFIX,        # NOT USED in template
        RUNTIME_EXEC_TOOL_NAME=DEFAULT_EXEC_TOOL_NAME,    # NOT USED in template
        TRUSTED_POLICY_PATH=None,                         # NOT USED in template
        TRUSTED_POLICY_URL=POLICY_RESOURCE_URI,           # USED (local constant, not server class constant)
    )
    return str(rendered)
```

**Self-check usage** (line 516):

```python
self.self_check(content)
# Which calls run_policy_source with:
input_payload=PolicyRequest(name=build_mcp_function(UI_MOUNT_PREFIX, "send_message"), arguments="{}")
```

#### Default Policy Constants Usage

**From `agent_server/src/agent_server/policies/default_policy.py`:**

```python
from mcp_infra.constants import RESOURCES_MOUNT_PREFIX, UI_MOUNT_PREFIX
from agent_server.mcp.ui.server import END_TURN_TOOL_NAME, SEND_MESSAGE_TOOL_NAME

UI_SEND = build_mcp_function(UI_MOUNT_PREFIX, SEND_MESSAGE_TOOL_NAME)
UI_END = build_mcp_function(UI_MOUNT_PREFIX, END_TURN_TOOL_NAME)

def decide(req: PolicyRequest) -> PolicyResponse:
    if req.name in (UI_SEND, UI_END):
        return PolicyResponse(decision=ApprovalDecision.ALLOW, rationale="UI communication")
    if server_matches(req.name, server=RESOURCES_MOUNT_PREFIX):
        return PolicyResponse(decision=ApprovalDecision.ALLOW, rationale="resource operations allowed")
    return PolicyResponse(decision=ApprovalDecision.ASK, rationale="default: ask")
```

**ACTUAL constants used by default policy:**

- ✅ `UI_MOUNT_PREFIX` - mount prefix (from `_shared/constants`)
- ✅ `RESOURCES_MOUNT_PREFIX` - mount prefix (from `_shared/constants`)
- ✅ `SEND_MESSAGE_TOOL_NAME` - **UI server tool constant!**
- ✅ `END_TURN_TOOL_NAME` - **UI server tool constant!**

#### Summary of Constants Usage

**Constants USED by baked-in default policy:**

- Mount prefixes: `UI_MOUNT_PREFIX`, `RESOURCES_MOUNT_PREFIX`
- UI server tool names: `SEND_MESSAGE_TOOL_NAME`, `END_TURN_TOOL_NAME`

**Constants currently DEFINED but NOT used by default policy:**

- Runtime/exec constants: `DEFAULT_EXEC_TOOL_NAME`, `CONTAINER_INFO_URI`
- Resources server constants: `SUBSCRIPTIONS_INDEX_URI`
- Chat server constants: `CHAT_HEAD_URI`, `CHAT_LAST_READ_URI`, etc.
- Loop server constants: `LOOP_MOUNT_PREFIX`
- Compositor meta constants: various URIs
- Approval policy engine's local constants (in `engine.py`)

**KEY FINDING:**

Default policy uses:

- ✅ **Mount prefix constants** (for all servers it checks)
- ✅ **UI server tool name constants** (for allow-list matching)
- ❌ **Runtime/exec constants** (not referenced)
- ❌ **Resources server tool/resource constants** (only mount prefix used for wildcard matching)
- ❌ **Chat, loop, compositor meta constants** (not referenced)

**Implication:**

While the default policy DOES use some server class constants (UI server tool names), it doesn't use constants from MOST servers. Many of the constants we're defining (runtime exec, resources, chat, etc.) are not used by the default policy.

**Test policies also use constants:** Test policies in `tests/agent/testdata/approval_policy/` use:

- `WellKnownTools.SEND_MESSAGE`, `WellKnownTools.SANDBOX_EXEC` (enum in `approvals.py`)
- `UI_MOUNT_PREFIX`, `SEATBELT_EXEC_MOUNT_PREFIX` (mount prefixes)
- Note: `WellKnownTools` is a StrEnum that duplicates tool name strings from server classes

**User policies might use more:** Custom policies could reference runtime exec, specific resources, etc. But the baked-in default and test policies use a minimal set.

---

## Summary Table

| Context                | Server Instances Available?     | Need Constants? | Refactoring Difficulty | Value |
| ---------------------- | ------------------------------- | --------------- | ---------------------- | ----- |
| **Test Fixtures**      | ✅ Yes (can reorder code!)      | ❌ **No!**      | Medium                 | High  |
| **Bootstrap Handlers** | ✅ Yes (servers mounted first!) | ❌ **No!**      | Easy                   | High  |
| **Prompt Templates**   | ✅ Yes (rendered in async!)     | ❌ **No!**      | Easy                   | High  |
| **Step Classes**       | ✅ Yes (can reorder code!)      | ❌ **No!**      | Medium                 | High  |
| **Policy Evaluation**  | ❌ No (Docker isolation)        | ✅ **Yes**      | Impossible             | N/A   |

---

## Conclusion

**ONLY 1 CONTEXT NEEDS CLASS CONSTANTS!** 🎉

### Three Major Refactoring Opportunities

**1. Bootstrap Helpers**

Servers are mounted BEFORE bootstrap construction:

```python
# Current (string literals):
def make_commit_bootstrap_calls(builder, server: str, ...):
    return [builder.call(server, "git_status", ...)]  # String literal

# Refactored (server instances):
def make_commit_bootstrap_calls(builder, mount_prefix: str, git_server: GitServer, ...):
    return [builder.call(mount_prefix, git_server.git_status_tool.name, ...)]  # From instance!
```

**2. Prompt Templates**

Templates rendered AFTER servers are mounted (inside async functions):

```python
# Current (no servers):
return render_prompt_template(
    "critic/prompts/critic_system.j2.md",
    compositor_instructions=compositor_instructions,
)

# Refactored (pass servers):
return render_prompt_template(
    "critic/prompts/critic_system.j2.md",
    compositor_instructions=compositor_instructions,
    runtime_server=runtime_server,  # ← Pass instance!
)

# Template uses: {{ runtime_server.exec_tool.name }}
```

**3. Test Fixtures**

Tests can reorder code to construct steps AFTER servers are mounted:

```python
# Current (steps before servers):
mock = make_step_runner(steps=[EchoCall("test")])
async with make_pg_compositor(...) as (mcp_client, _):
    agent = await Agent.create(...)

# Refactored (servers before steps):
async with make_pg_compositor(...) as (mcp_client, compositor):
    echo_server = compositor.get_inproc_server("echo")
    mock = make_step_runner(steps=[EchoCall(echo_server, "test")])
    agent = await Agent.create(...)
```

**This leaves ONLY 1 context with architectural need for class constants:**

1. **Policy Evaluation** - Runs in isolated Docker (architectural constraint)

**Current state:** Other contexts still use constants due to implementation, but CAN be refactored.

**HOWEVER:** Default policy uses only a SUBSET of server constants!

**Audit findings:**

- Policy eval engine uses: `UI_MOUNT_PREFIX` (for self-check test function name)
- Default policy uses:
  - ✅ Mount prefixes: `UI_MOUNT_PREFIX`, `RESOURCES_MOUNT_PREFIX`
  - ✅ **UI server tool names**: `SEND_MESSAGE_TOOL_NAME`, `END_TURN_TOOL_NAME`
  - ❌ Runtime/exec constants (not used)
  - ❌ Resources server tool/resource constants (only mount prefix for wildcard matching)
  - ❌ Chat, loop, compositor meta constants (not used)

**This means:**

- Default policy DOES use constants from UI server (for allow-list matching)
- Default policy does NOT use constants from most other servers (runtime, chat, loop, etc.)
- User policies MIGHT use more constants, but baked-in default uses minimal set

**Minimal pattern (for servers actually used by default policy):**

```python
# Shared constants (in _shared/constants.py) - for policy eval + default policy
UI_MOUNT_PREFIX: Final[str] = "ui"
RESOURCES_MOUNT_PREFIX: Final[str] = "resources"
RUNTIME_MOUNT_PREFIX: Final[str] = "runtime"

# UI Server - HAS class constants (used by default policy)
class UiServer:
    SEND_MESSAGE_TOOL_NAME: ClassVar[str] = "send_message"
    END_TURN_TOOL_NAME: ClassVar[str] = "end_turn"

    def __init__(self):
        @self.tool(name=self.SEND_MESSAGE_TOOL_NAME)
        async def send_message(...): ...
        self.send_message_tool = send_message

# Runtime Server - NO class constants needed (not used by default policy)
class RuntimeServer:
    def __init__(self):
        @self.tool(name="exec")  # Can use string literal
        async def exec_impl(...): ...
        self.exec_tool = exec_impl
```

**Note on WellKnownTools enum (NEEDS REFACTORING):**
`agent_server/src/agent_server/approvals.py` defines a `WellKnownTools` StrEnum:

```python
class WellKnownTools(StrEnum):
    SEND_MESSAGE = "send_message"      # duplicates UiServer.SEND_MESSAGE_TOOL_NAME
    END_TURN = "end_turn"              # duplicates UiServer.END_TURN_TOOL_NAME
    SANDBOX_EXEC = "sandbox_exec"      # duplicates seatbelt exec constant
```

**This is redundant!** Policies CAN import from server classes (the runtime Docker image has `adgn` installed):

```python
# Default policy already does this correctly:
from agent_server.mcp.ui.server import END_TURN_TOOL_NAME, SEND_MESSAGE_TOOL_NAME

# Test policies use WellKnownTools unnecessarily - could import from servers directly
```

**Refactoring needed (low priority):**

1. Eliminate `WellKnownTools` enum (redundant with server class constants)
2. Update test policies to import constants from server classes instead
3. This removes duplication and ensures one source of truth

**Note:** Only 3 entries, test-only usage, minimal impact - can be deferred.

**Revised Understanding:**

- Policy eval needs **mount prefix constants** (in `_shared/constants`)
- Default/test policies need **tool name constants** for:
  - UI server: `SEND_MESSAGE`, `END_TURN` (used by default policy)
  - Seatbelt exec: `SANDBOX_EXEC` (used by test policies)
- These constants exist in **two places**: server classes AND `WellKnownTools` enum (duplication!)
- Other servers (runtime, chat, resources, etc.) don't need constants unless user policies reference them

**Final Recommendation (Priority Order):**

1. ✅ **HIGH: Bootstrap helpers** - Replace string literals with server instances (easy, fixes DoD violation)
2. ✅ **HIGH: Test fixtures** - Expose server instances, update step constructors (medium-high effort, high value)
3. ✅ **MEDIUM: Dual-access pattern** - Complete Stage 3 (remove `type: ignore`, add class constants + instance attributes)
4. ✅ **LOW: WellKnownTools enum** - Eliminate and update test policies (low priority, can defer)
5. ✅ **LOW: Audit unused constants** - Identify dead constants from runtime, resources, chat, loop

**Note on templates:** Current templates don't reference tool names - no refactoring needed unless new templates require it.
