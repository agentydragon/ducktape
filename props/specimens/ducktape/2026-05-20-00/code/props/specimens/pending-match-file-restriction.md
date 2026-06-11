# Issues Missing `match_file_restriction`

Total: 204 single-file TP occurrences without `match_file_restriction`.

For detailed per-occurrence analysis with validation proofs, use:
`/narrow_matchability <snapshot_slug>`

## ducktape/2026-01-17-00 (22)

### `dead-code.yaml` / `occ-9`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/dead-code.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/agent_server/mcp/approval_policy/clients.py#L1-L22)

File: `agent_server/mcp/approval_policy/clients.py` (L1-22)

> Code that is defined but never used. Should be deleted to reduce maintenance
> burden and avoid confusion.
>
> **Note:** Dead module — `PolicyReaderStub` and `PolicyApproverStub` defined but never imported.

```
>>>    1: from __future__ import annotations
>>>    2:
>>>    3: from agent_server.mcp.approval_policy.engine import DecideProposalArgs, SetPolicyTextArgs
>>>    4: from agent_server.policies.policy_types import PolicyRequest, PolicyResponse
>>>    5: from mcp_infra.stubs.server_stubs import ServerStub
>>>    6:
>>>    7:
>>>    8: class PolicyReaderStub(ServerStub):
>>>    9:     """Typed stub for the approval policy reader MCP server."""
>>>   10:
>>>   11:     async def evaluate_policy(self, input: PolicyRequest) -> PolicyResponse:
>>>   12:         raise NotImplementedError  # Auto-wired at runtime
>>>   13:
>>>   14:
>>>   15: class PolicyApproverStub(ServerStub):
>>>   16:     """Typed stub for the approval policy approver MCP server."""
>>>   17:
>>>   18:     async def set_policy_text(self, input: SetPolicyTextArgs) -> None:
>>>   19:         raise NotImplementedError  # Auto-wired at runtime
>>>   20:
>>>   21:     async def decide_proposal(self, input: DecideProposalArgs) -> None:
>>>   22:         raise NotImplementedError  # Auto-wired at runtime
```

### `dead-fixtures.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/dead-fixtures.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/agent_core_testing/fixtures.py#L88-L102)

File: `agent_core_testing/fixtures.py` (L88, L102)

> Multiple pytest fixtures defined in shared locations have zero test
> consumers and should be deleted.
>
> **Note:** `make_fake_openai` and `make_capturing_client` — no test uses either

```
      85:
      86:
      87: @pytest.fixture
>>>   88: def make_fake_openai() -> Callable[[Iterable[ResponsesResult]], FakeOpenAIModel]:
      89:     """Factory to create FakeOpenAIModel instances from response sequences.
      90:
      91:     Usage:
   ...
      99:
     100:
     101: @pytest.fixture
>>>  102: def make_capturing_client():
     103:     """Factory to create a CapturingOpenAIModel wrapping FakeOpenAIModel.
     104:
     105:     Usage:
```

### `dead-fixtures.yaml` / `occ-1`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/dead-fixtures.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/agent_server/conftest.py#L219-L262)

File: `agent_server/conftest.py` (L219, L231, L239, L251, L257, L262)

> Multiple pytest fixtures defined in shared locations have zero test
> consumers and should be deleted.
>
> **Note:** policy_ui_send_message_allow, policy_failing_tests, policy_version_test, policy_invalid_syntax, policy_context_checking, policy_const — no test uses any

```
     216:
     217:
     218: @pytest.fixture
>>>  219: def policy_ui_send_message_allow() -> str:
     220:     result: str = make_policy(
     221:         decision_expr="ApprovalDecision.ALLOW",
     222:         server="ui",
   ...
     228:
     229:
     230: @pytest.fixture
>>>  231: def policy_failing_tests() -> str:
     232:     return str(fetch_policy("failing_tests"))
     233:
     234:
   ...
     236:
     237:
     238: @pytest.fixture
>>>  239: def policy_version_test() -> str:
     240:     result: str = make_policy(
     241:         decision_expr="ApprovalDecision.ALLOW",
     242:         server="ui",
   ...
     248:
     249:
     250: @pytest.fixture
>>>  251: def policy_invalid_syntax() -> str:
     252:     # Intentionally invalid Python
     253:     return "class ApprovalPolicy:\n    '''invalid'''\n    def decide(self, ctx):\n        return (ApprovalDecision.ALLOW, 'ok'\n"
     254:
     255:
     256: @pytest.fixture
>>>  257: def policy_context_checking() -> str:
     258:     return str(fetch_policy("context_checking"))
     259:
     260:
     261: @pytest.fixture
>>>  262: def policy_const() -> str:
     263:     return str(fetch_policy("const"))
     264:
     265:
```

### `dead-fixtures.yaml` / `occ-2`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/dead-fixtures.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/agent_server/conftest.py#L291-L359)

File: `agent_server/conftest.py` (L291, L335, L348, L359)

> Multiple pytest fixtures defined in shared locations have zero test
> consumers and should be deleted.
>
> **Note:** create_live_agent, patch_agent_build_client, agent_app_client, agent_test_client — no test uses any; last two form a chain

```
     288:
     289: # Helper: create a live agent via HTTP on a TestClient and return its id
     290: @pytest.fixture
>>>  291: def create_live_agent():
     292:     def _create(client, *, specs: McpServerSpecs | None = None) -> str:
     293:         specs = specs or {}
     294:         # Split into typed JSON specs vs runtime slot specs
   ...
     332:
     333:
     334: @pytest.fixture
>>>  335: def patch_agent_build_client(monkeypatch: pytest.MonkeyPatch) -> Callable[[OpenAIModelProto], None]:
     336:     """Return a function to patch container.build_client to a provided fake client.
     337:
     338:     Keeps model patching independent from agent creation, so tests can opt-in.
   ...
     345:
     346:
     347: @pytest.fixture
>>>  348: def agent_app_client():
     349:     """Yield a (app, client) pair for the UI server with static assets not required.
     350:
     351:     Ensures a consistent pattern across tests, avoiding repeated create_app/TestClient boilerplate.
   ...
     356:
     357:
     358: @pytest.fixture
>>>  359: def agent_test_client(agent_app_client):
     360:     """Return just the TestClient for agent server tests.
     361:
     362:     Use this when you only need the client and not the app.
```

### `dead-fixtures.yaml` / `occ-3`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/dead-fixtures.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/mcp_infra/seatbelt/conftest.py#L60-L76)

File: `mcp_infra/seatbelt/conftest.py` (L60, L76)

> Multiple pytest fixtures defined in shared locations have zero test
> consumers and should be deleted.
>
> **Note:** `cat_process` and `run_async` — no test uses either

```
      57:
      58:
      59: @pytest.fixture
>>>   60: async def cat_process(require_sandbox_exec, allow_all_policy: SBPLPolicy):
      61:     p = await apopen(["/bin/sh", "-c", "cat"], allow_all_policy, trace=True)
      62:     try:
      63:         yield p
   ...
      73:
      74:
      75: @pytest.fixture
>>>   76: def run_async(require_sandbox_exec):
      77:     async def _run(policy: SBPLPolicy, argv: list[str], *, trace: bool = False):
      78:         rr = await run_sandboxed_async(
      79:             policy, argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, trace=trace
```

### `dead-fixtures.yaml` / `occ-4`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/dead-fixtures.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/mcp_infra/testing/fixtures.py#L103)

File: `mcp_infra/testing/fixtures.py` (L103)

> Multiple pytest fixtures defined in shared locations have zero test
> consumers and should be deleted.
>
> **Note:** `resource_capture` — no test uses it

```
     100:
     101:
     102: @pytest.fixture
>>>  103: def resource_capture() -> ResourceUpdatedCapture:
     104:     """Fresh ResourceUpdatedCapture instance for each test."""
     105:     return ResourceUpdatedCapture()
     106:
```

### `dead-fixtures.yaml` / `occ-5`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/dead-fixtures.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/agent_core_testing/fixtures.py#L119-L146)

File: `agent_core_testing/fixtures.py` (L119-146)

> Multiple pytest fixtures defined in shared locations have zero test
> consumers and should be deleted.
>
> **Note:** `make_test_agent` — no test uses it

```
     116:
     117:
     118: @pytest.fixture
>>>  119: def make_test_agent(responses_factory: ResponsesFactory):
>>>  120:     """Factory to create Agent backed by FakeOpenAIModel with canned responses.
>>>  121:
>>>  122:     Returns (agent, fake_client) tuple so tests can inspect the client after run.
>>>  123:
>>>  124:     Usage:
>>>  125:         agent, client = await make_test_agent(
>>>  126:             mcp_client,
>>>  127:             [responses_factory.make_assistant_message("done")],
>>>  128:         )
>>>  129:         result = await agent.run("hi")
>>>  130:         assert client.calls == 1
>>>  131:     """
>>>  132:
>>>  133:     async def _make(mcp_client, responses, *, handlers=(), system="test", tool_policy=None, **kwargs):
>>>  134:         fake_model = FakeOpenAIModel(list(responses))
>>>  135:         client = CapturingOpenAIModel(fake_model)  # Wrap to enable .captured
>>>  136:         # Minimal defaults - tests should be explicit about their needs
>>>  137:         if not handlers:
>>>  138:             handlers = [BaseHandler()]  # Minimal no-op handler (Agent requires at least one)
>>>  139:         if tool_policy is None:
>>>  140:             tool_policy = RequireAnyTool()
>>>  141:         agent = await Agent.create(
>>>  142:             mcp_client=mcp_client, client=client, handlers=handlers, tool_policy=tool_policy, **kwargs
>>>  143:         )
>>>  144:         return agent, client
>>>  145:
>>>  146:     return _make
     147:
     148:
     149: # ---- MCP fixtures ----
```

### `dead-fixtures.yaml` / `occ-6`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/dead-fixtures.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/agent_server/conftest.py#L424-L437)

File: `agent_server/conftest.py` (L424-437)

> Multiple pytest fixtures defined in shared locations have zero test
> consumers and should be deleted.
>
> **Note:** `failing_server` — no test uses it

```
     421:
     422:
     423: @pytest.fixture
>>>  424: def failing_server() -> EnhancedFastMCP:
>>>  425:     """EnhancedFastMCP server with a tool that returns an error payload."""
>>>  426:     # Workaround: Pass version="test" to skip slow importlib.metadata.version() lookup
>>>  427:     # that hangs on os.stat() in Nix environment. Without this, MCP server initialization
>>>  428:     # would call pkg_version("mcp") which triggers filesystem operations that timeout.
>>>  429:     mcp = EnhancedFastMCP("editor", version="test")
>>>  430:
>>>  431:     @mcp.flat_model()
>>>  432:     def fail(input: _FailInput) -> dict[str, Any]:
>>>  433:         # Return error payload in structured_content (not raise ToolError)
>>>  434:         # The test expects ok=False, error="boom" in structured_content
>>>  435:         return {"ok": False, "error": "boom"}
>>>  436:
>>>  437:     return mcp
     438:
     439:
     440: @pytest.fixture
```

### `dead-fixtures.yaml` / `occ-7`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/dead-fixtures.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/claude/claude_hooks/conftest.py#L163-L170)

File: `claude/claude_hooks/conftest.py` (L163-170)

> Multiple pytest fixtures defined in shared locations have zero test
> consumers and should be deleted.
>
> **Note:** `mock_context` — no test uses it

```
     160:
     161:
     162: @pytest.fixture
>>>  163: def mock_context(tmp_path: Path) -> HookContext:
>>>  164:     """Create a mock HookContext for testing."""
>>>  165:     return HookContext(
>>>  166:         hook_name="test_hook",
>>>  167:         hook_event="PostToolUse",
>>>  168:         session_id=UUID("12345678-1234-5678-9abc-123456789abc"),
>>>  169:         cwd=tmp_path,
>>>  170:     )
     171:
     172:
     173: @pytest.fixture
```

### `early-bailout.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/early-bailout.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/wt/server/handlers/worktree_handler.py#L193-L205)

File: `wt/server/handlers/worktree_handler.py` (L193-205)

> `worktree_get_by_name` uses if/else assigning to `result` then returns it.
> The else branch is the "not found" early-exit case. Use early return to
> flatten the happy path and eliminate the intermediate variable.

```
     190: async def worktree_get_by_name(
     191:     index: WorktreeIndexService, config: Configuration, params: WorktreeGetByNameParams
     192: ) -> WorktreeGetByNameResult:
>>>  193:     found_worktree = _resolve_worktree_name_to_info(index, params.name)
>>>  194:     if found_worktree:
>>>  195:         worktree_name = (
>>>  196:             MAIN_WORKTREE_DISPLAY_NAME
>>>  197:             if found_worktree.path.resolve() == config.main_repo.resolve()
>>>  198:             else found_worktree.path.name
>>>  199:         )
>>>  200:         result = WorktreeGetByNameResult(
>>>  201:             wtid=make_worktree_id(worktree_name), name=worktree_name, exists=True, absolute_path=found_worktree.path
>>>  202:         )
>>>  203:     else:
>>>  204:         result = WorktreeGetByNameResult(wtid=None, name=None, exists=False, absolute_path=None)
>>>  205:     return result
```

### `inline-pr-info.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/inline-pr-info.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/wt/client/view_formatter.py#L195-L221)

File: `wt/client/view_formatter.py` (L195-213, L221)

> `pr_info` is initialized as `""` (line 195), conditionally reassigned inside
> an `if pr_link:` block (lines 207-213), then used once (line 221). This
> widens the scope of mutable state. Instead, build `pr_parts` unconditionally
> as `[pr_link]` if truthy (else `[]`), append status/changes, and inline
> `" ".join(pr_parts)` at the use site. Eliminates the `pr_info` variable and
> the `if` block.

```
     192:         # Build table data
     193:         table_data = []
     194:         for name, status in sorted_items:
>>>  195:             pr_info = ""
>>>  196:             pr_link = self._get_pr_link_column(status)
>>>  197:             pr_status = self._get_pr_status_column(status)
>>>  198:             pr_changes = self._get_pr_changes_column(status)
>>>  199:             state_map = {
>>>  200:                 "running": "running",
>>>  201:                 "restarting": "restarting",
>>>  202:                 "failed": "failed",
>>>  203:                 "stopped": "stopped",
>>>  204:                 "starting": "starting",
>>>  205:             }
>>>  206:             state = state_map.get(status.gitstatusd_state or "", "")
>>>  207:             if pr_link:
>>>  208:                 pr_parts = [pr_link]
>>>  209:                 if pr_status:
>>>  210:                     pr_parts.append(pr_status)
>>>  211:                 if pr_changes:
>>>  212:                     pr_parts.append(pr_changes)
>>>  213:                 pr_info = " ".join(pr_parts)
     214:
     215:             table_data.append(
     216:                 [
   ...
     218:                     (status.commit_info.short_hash if status.commit_info else "ERROR"),
     219:                     self._work_status_text(status),
     220:                     state,
>>>  221:                     pr_info,
     222:                 ]
     223:             )
     224:
```

### `inline-trivial-var.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/inline-trivial-var.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/wt/server/handlers/worktree_handler.py#L44-L47)

File: `wt/server/handlers/worktree_handler.py` (L44, L47)

> `worktree_id` is assigned and used exactly once. Inline it:
> `wtid=make_worktree_id(worktree_name)`.

```
      41:         if info.is_main:
      42:             continue
      43:         worktree_name = info.path.name
>>>   44:         worktree_id = make_worktree_id(worktree_name)
      45:         worktrees.append(
      46:             WorktreeInfo(
>>>   47:                 wtid=worktree_id,
      48:                 name=worktree_name,
      49:                 absolute_path=info.path,
      50:                 branch_name=info.branch,
```

### `inverted-step-dep.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/inverted-step-dep.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/agent_core_testing/responses.py#L46-L502)

File: `agent_core_testing/responses.py` (L46, L134-169, L493-502)

> `responses.py` is the low-level response-building layer. `steps.py` defines
> the `Step` Protocol and concrete step implementations that consume
> `ResponsesFactory` from `responses.py`. The dependency should be
> steps → responses (higher layer depends on lower layer).
>
> Instead, `responses.py` imports `Step` from `steps.py` (line 46) because
> `StepRunner` (line 134) and the `make_step_runner` fixture (line 493) live
> in `responses.py`. This inverts the layering: the lower-level module depends
> on the higher-level one.
>
> Fix: move `StepRunner` and `make_step_runner` into `steps.py`. The dependency
> becomes one-directional: steps → responses.

```
      43: )
      44:
      45: if TYPE_CHECKING:
>>>   46:     from agent_core_testing.steps import Step
      47:
      48:
      49: logger = logging.getLogger(__name__)
   ...
     131:         return self.tool_call(mounted.tool_name(tool), arguments.model_dump(mode="json"), call_id)
     132:
     133:
>>>  134: class StepRunner(OpenAIModelProto):
>>>  135:     """Step-based OpenAI mock that executes declarative test steps.
>>>  136:
>>>  137:     Implements OpenAIModelProto directly, so can be used as the client parameter
>>>  138:     to agent functions without any wrapping.
>>>  139:
>>>  140:     Usage:
>>>  141:         runner = make_step_runner(steps=[AssistantMessage("Done")])
>>>  142:         result = await agent.run(..., client=runner)
>>>  143:
>>>  144:     Debug logging:
>>>  145:         To see step execution with timestamps (for timeout tuning):
>>>  146:             pytest --log-cli-level=DEBUG tests/path/to/test.py
>>>  147:     """
>>>  148:
>>>  149:     def __init__(self, factory: ResponsesFactory, steps: Sequence[Step]) -> None:
>>>  150:         self.factory: ResponsesFactory = factory
>>>  151:         self.steps: Sequence[Step] = steps
>>>  152:         self.turn: int = 0
>>>  153:         self.model = "test-model"
>>>  154:
>>>  155:     @property
>>>  156:     def current_step_index(self) -> int:
>>>  157:         """Current step index (0-based). Alias for turn for clarity."""
>>>  158:         return self.turn
>>>  159:
>>>  160:     async def responses_create(self, req: ResponsesRequest) -> ResponsesResult:
>>>  161:         """Execute current step and advance. Implements OpenAIModelProto."""
>>>  162:         if self.turn >= len(self.steps):
>>>  163:             pytest.fail(f"Exceeded {len(self.steps)} expected turns (got turn {self.turn + 1})")
>>>  164:         step = self.steps[self.turn]
>>>  165:         step_type = type(step).__name__
>>>  166:         logger.debug("Step %d/%d (%s)", self.turn + 1, len(self.steps), step_type)
>>>  167:         result = step.execute(req, self.factory)
>>>  168:         self.turn += 1
>>>  169:         return result
     170:
     171:
     172: # Type for generator that yields responses and receives requests
   ...
     490:
     491:
     492: @pytest.fixture
>>>  493: def make_step_runner(responses_factory: ResponsesFactory):
>>>  494:     """Factory fixture that creates step runners.
>>>  495:
>>>  496:     Returns a factory function that creates StepRunner instances.
>>>  497:     """
>>>  498:
>>>  499:     def _make(steps: Sequence[Step]) -> StepRunner:
>>>  500:         return StepRunner(factory=responses_factory, steps=steps)
>>>  501:
>>>  502:     return _make
```

### `misplaced-test-fixtures.yaml` / `occ-1`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/misplaced-test-fixtures.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/agent_server/conftest.py#L392-L455)

File: `agent_server/conftest.py` (L392-455)

> Several test fixtures in `agent_core_testing/fixtures.py` are only used by
> `agent_core/test_tool_execution.py` and should be moved into that test
> module: `ValidationServer` (line 292), `validation_server` (line 310),
> `_FailInput` (line 315), `FAIL_TOOL_NAME` (line 322),
> `error_payload_server` (line 326), `_EmptyInput` (line 181),
> `_SlowOutput` (line 185), and `slow_server` (line 194).
>
> Additionally, `agent_server/conftest.py` has duplicate dead copies of
> `ValidationServer` (line 392), `_FailInput` (line 417),
> `validation_server` (line 412), and `slow_server` (line 441) — no test
> in `agent_server/` uses any of them.
>
> **Note:** Duplicate ValidationServer/\_FailInput/validation_server/slow_server — dead code

```
     389: # ---- Server fixtures for tool error and parallel tests ------------------------
     390:
     391:
>>>  392: class ValidationServer(EnhancedFastMCP):
>>>  393:     """EnhancedFastMCP server with a tool that validates input strictly."""
>>>  394:
>>>  395:     # Tool attribute (assigned in __init__)
>>>  396:     send_message_tool: FunctionTool
>>>  397:
>>>  398:     def __init__(self):
>>>  399:         super().__init__("validator")
>>>  400:
>>>  401:         def send_message(input: SendMessageInput) -> dict[str, Any]:
>>>  402:             """Send a message with mime type validation."""
>>>  403:             # Reject text/plain to test error handling
>>>  404:             if input.mime == "text/plain":
>>>  405:                 raise ToolError("Validation error: Only text/markdown is supported, not text/plain")
>>>  406:             return {"ok": True, "message": input.content}
>>>  407:
>>>  408:         self.send_message_tool = self.flat_model()(send_message)
>>>  409:
>>>  410:
>>>  411: @pytest.fixture
>>>  412: def validation_server() -> ValidationServer:
>>>  413:     """ValidationServer with typed tool access."""
>>>  414:     return ValidationServer()
>>>  415:
>>>  416:
>>>  417: class _FailInput(OpenAIStrictModeBaseModel):
>>>  418:     """Input for fail tool (test fixture)."""
>>>  419:
>>>  420:     x: int
>>>  421:
>>>  422:
>>>  423: @pytest.fixture
>>>  424: def failing_server() -> EnhancedFastMCP:
>>>  425:     """EnhancedFastMCP server with a tool that returns an error payload."""
>>>  426:     # Workaround: Pass version="test" to skip slow importlib.metadata.version() lookup
>>>  427:     # that hangs on os.stat() in Nix environment. Without this, MCP server initialization
>>>  428:     # would call pkg_version("mcp") which triggers filesystem operations that timeout.
>>>  429:     mcp = EnhancedFastMCP("editor", version="test")
>>>  430:
>>>  431:     @mcp.flat_model()
>>>  432:     def fail(input: _FailInput) -> dict[str, Any]:
>>>  433:         # Return error payload in structured_content (not raise ToolError)
>>>  434:         # The test expects ok=False, error="boom" in structured_content
>>>  435:         return {"ok": False, "error": "boom"}
>>>  436:
>>>  437:     return mcp
>>>  438:
>>>  439:
>>>  440: @pytest.fixture
>>>  441: def slow_server() -> FastMCP:
>>>  442:     """FastMCP server with two slow async tools for parallel call testing."""
>>>  443:     mcp = FastMCP("dummy")
>>>  444:
>>>  445:     @mcp.tool()
>>>  446:     async def slow() -> dict[str, Any]:
>>>  447:         await asyncio.sleep(0.30)
>>>  448:         return {"ok": True, "tool": "slow", "args": {}}
>>>  449:
>>>  450:     @mcp.tool()
>>>  451:     async def slow2() -> dict[str, Any]:
>>>  452:         await asyncio.sleep(0.30)
>>>  453:         return {"ok": True, "tool": "slow2", "args": {}}
>>>  454:
>>>  455:     return mcp
     456:
     457:
     458: # ---- UI reducer/history test fixtures ----------------------------------------
```

### `missing-lifespan-in-tests.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/missing-lifespan-in-tests.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/gatelet/server/conftest.py#L147-L159)

File: `gatelet/server/conftest.py` (L147-159)

> The test client fixture bypasses FastAPI's lifespan context, manually initializing
> CSRF config and templates instead of triggering proper startup. This causes routes
> registered in lifespan (like `/k/{key}/`, `/s/{session}/`) to not exist during tests,
> resulting in spurious 404 errors.
>
> Use `asgi-lifespan.LifespanManager` to properly trigger app startup/shutdown:
>
> ```python
> from asgi_lifespan import LifespanManager
> async with LifespanManager(app) as manager:
>     async with AsyncClient(transport=ASGITransport(app=manager.app), ...) as client:
>         yield client
> ```

```
     144:     def override_settings() -> Settings:
     145:         return test_settings
     146:
>>>  147:     # Initialize CSRF protection for tests (normally done in lifespan)
>>>  148:     _init_csrf_config(test_settings.security.csrf_secret)
>>>  149:
>>>  150:     # Initialize templates (normally done in lifespan)
>>>  151:     app.state.templates = Jinja2Templates(directory=BASE_DIR / "templates")
>>>  152:     app.state.templates.env.globals.update({"max": max, "min": min})
>>>  153:
>>>  154:     app.dependency_overrides[get_db_session] = override_db
>>>  155:     app.dependency_overrides[get_settings] = override_settings
>>>  156:     async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
>>>  157:         yield client
>>>  158:     app.dependency_overrides.pop(get_db_session, None)
>>>  159:     app.dependency_overrides.pop(get_settings, None)
     160:
     161:
     162: @pytest_asyncio.fixture
```

### `mixed-api-domain-types.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/mixed-api-domain-types.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/wt/shared/github_models.py#L1-L189)

File: `wt/shared/github_models.py` (L1-189)

> `github_models.py` mixes GitHub API boundary types with internal domain
> types in one flat module. API types that directly mirror GitHub response
> shapes (`PRState`, `PullRequestSearch`, `PullRequestList`,
> `GitHubPRResponse`, `HasBasicPR`, `GitHubError`) live alongside internal
> domain types (`PRStatus`, `PRData`, `PRInfo`, `PRInfoRepr`,
> `PRMergeability`, `coerce_prdata`). The API types use `Field(alias=...)`
> and match GitHub field names; the domain types use different field names
> (e.g. `pr_number` vs `number`) and add derived concepts.
>
> Separate the GitHub API shapes into their own module (e.g.
> `github_api_types.py`) so the boundary is explicit and domain types can
> evolve independently.

```
>>>    1: from __future__ import annotations
>>>    2:
>>>    3: from dataclasses import dataclass
>>>    4: from datetime import datetime
>>>    5: from enum import StrEnum
>>>    6: from typing import Any, Protocol, runtime_checkable
>>>    7:
>>>    8: from pydantic import BaseModel, Field
>>>    9:
>>>   10:
>>>   11: class GitHubError(Exception):
>>>   12:     pass
>>>   13:
>>>   14:
>>>   15: class PRStatus(StrEnum):
>>>   16:     MERGED = "MERGED"
>>>   17:     CLOSED = "CLOSED"
>>>   18:     OPEN_MERGEABLE = "OPEN_MERGEABLE"
>>>   19:     OPEN_CONFLICTING = "OPEN_CONFLICTING"
>>>   20:     OPEN_UNKNOWN = "OPEN_UNKNOWN"
>>>   21:
>>>   22:     @property
>>>   23:     def is_merged(self) -> bool:
>>>   24:         return self == PRStatus.MERGED
>>>   25:
>>>   26:     @property
>>>   27:     def is_open(self) -> bool:
>>>   28:         return self.name.startswith("OPEN_")
>>>   29:
>>>   30:     @property
>>>   31:     def is_closed(self) -> bool:
>>>   32:         return self == PRStatus.CLOSED
>>>   33:
>>>   34:     @property
>>>   35:     def display_text(self) -> str:
>>>   36:         if self == PRStatus.MERGED:
>>>   37:             return "merged"
>>>   38:         if self == PRStatus.CLOSED:
>>>   39:             return "closed"
>>>   40:         if self == PRStatus.OPEN_MERGEABLE:
>>>   41:             return "can merge"
>>>   42:         if self == PRStatus.OPEN_CONFLICTING:
>>>   43:             return "conflict"
>>>   44:         if self == PRStatus.OPEN_UNKNOWN:
>>>   45:             return "open"
>>>   46:         return self.value.lower()
>>>   47:
>>>   48:
>>>   49: class PRState(StrEnum):
>>>   50:     OPEN = "open"
>>>   51:     CLOSED = "closed"
>>>   52:     MERGED = "merged"
>>>   53:
>>>   54:     @property
>>>   55:     def is_merged(self) -> bool:
>>>   56:         return self == PRState.MERGED
>>>   57:
>>>   58:
>>>   59: class PRMergeability(StrEnum):
>>>   60:     CONFLICTING = "CONFLICTING"
>>>   61:     UNKNOWN = "UNKNOWN"
>>>   62:
>>>   63:
>>>   64: class PullRequestSearch(BaseModel):
>>>   65:     number: int
>>>   66:     title: str
>>>   67:     state: PRState
>>>   68:     url: str
>>>   69:
>>>   70:
>>>   71: class PullRequestList(BaseModel):
>>>   72:     number: int
>>>   73:     head_ref_name: str = Field(alias="headRefName")
>>>   74:     state: PRState
>>>   75:     title: str
>>>   76:     merged_at: str | None = Field(None, alias="mergedAt")
>>>   77:
>>>   78:
>>>   79: class PRData(BaseModel):
>>>   80:     pr_number: int
>>>   81:     pr_state: PRState
>>>   82:     draft: bool = False
>>>   83:     mergeable: bool | None = None
>>>   84:     merged_at: str | None = None
>>>   85:     additions: int | None = None
>>>   86:     deletions: int | None = None
>>>   87:
>>>   88:
>>>   89: class GitHubPRResponse(BaseModel):
>>>   90:     """Raw GitHub PR API response data"""
>>>   91:
>>>   92:     number: int
>>>   93:     state: PRState
>>>   94:     title: str
>>>   95:     draft: bool = False
>>>   96:     mergeable: bool | None = None
>>>   97:     merged_at: str | None = None
>>>   98:     additions: int | None = None
>>>   99:     deletions: int | None = None
>>>  100:
>>>  101:     @classmethod
>>>  102:     def from_github_pr(cls, pr) -> GitHubPRResponse:
>>>  103:         """Create from PyGithub PR object"""
>>>  104:         return cls(
>>>  105:             number=pr.number,
>>>  106:             state=pr.state,
>>>  107:             title=pr.title,
>>>  108:             draft=pr.draft,
>>>  109:             mergeable=pr.mergeable,
>>>  110:             merged_at=pr.merged_at.isoformat() if pr.merged_at else None,
>>>  111:             additions=pr.additions,
>>>  112:             deletions=pr.deletions,
>>>  113:         )
>>>  114:
>>>  115:
>>>  116: class PRInfoRepr(BaseModel):
>>>  117:     branch: str
>>>  118:     pr_data: PRData | None = None
>>>  119:     gh_error: str | None = None
>>>  120:
>>>  121:
>>>  122: def coerce_prdata(src: Any) -> PRData:
>>>  123:     if isinstance(src, PRData):
>>>  124:         return src
>>>  125:     if isinstance(src, GitHubPRResponse):
>>>  126:         return PRData(
>>>  127:             pr_number=src.number,
>>>  128:             pr_state=PRState(src.state),
>>>  129:             draft=src.draft,
>>>  130:             mergeable=src.mergeable,
>>>  131:             merged_at=src.merged_at,
>>>  132:             additions=src.additions,
>>>  133:             deletions=src.deletions,
>>>  134:         )
>>>  135:     if isinstance(src, dict):
>>>  136:         num = src["pr_number"] if "pr_number" in src else src["number"]
>>>  137:         st = src.get("pr_state")
>>>  138:         raw_state = st if st is not None else src.get("state")
>>>  139:         if raw_state is None:
>>>  140:             raise KeyError("state")
>>>  141:         state = raw_state if isinstance(raw_state, PRState) else PRState(str(raw_state))
>>>  142:         return PRData(
>>>  143:             pr_number=int(num),
>>>  144:             pr_state=state,
>>>  145:             draft=bool(src.get("draft", False)),
>>>  146:             mergeable=src.get("mergeable"),
>>>  147:             merged_at=src.get("merged_at"),
>>>  148:             additions=src.get("additions"),
>>>  149:             deletions=src.get("deletions"),
>>>  150:         )
>>>  151:     raise TypeError("Unsupported PR data type")
>>>  152:
>>>  153:
>>>  154: @runtime_checkable
>>>  155: class HasBasicPR(Protocol):  # minimal protocol for PyGithub-like PR (read-only properties OK)
>>>  156:     @property
>>>  157:     def number(self) -> int: ...
>>>  158:
>>>  159:     @property
>>>  160:     def state(self) -> str: ...
>>>  161:
>>>  162:     @property
>>>  163:     def title(self) -> str: ...
>>>  164:
>>>  165:     @property
>>>  166:     def draft(self) -> bool: ...
>>>  167:
>>>  168:     @property
>>>  169:     def mergeable(self) -> bool | None: ...
>>>  170:
>>>  171:     @property
>>>  172:     def merged_at(self) -> datetime | None: ...
>>>  173:
>>>  174:     @property
>>>  175:     def additions(self) -> int | None: ...
>>>  176:
>>>  177:     @property
>>>  178:     def deletions(self) -> int | None: ...
>>>  179:
>>>  180:
>>>  181: @dataclass
>>>  182: class PRInfo:
>>>  183:     branch: str
>>>  184:     pr_data: PRData | None = None
>>>  185:     github_pr: HasBasicPR | None = None  # runtime object, not serialized
>>>  186:     gh_error: str | None = None
>>>  187:
>>>  188:     def to_repr(self) -> PRInfoRepr:
>>>  189:         return PRInfoRepr(branch=self.branch, pr_data=self.pr_data, gh_error=self.gh_error)
```

### `redundant-structured-content-test.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/redundant-structured-content-test.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/agent_core/test_tool_execution.py#L95-L120)

File: `agent_core/test_tool_execution.py` (L95-120)

> `test_app_level_error_payload_surfaced_in_structured_content` (line 95)
> tests that a successful MCP result with `{"ok": False, "error": "boom"}`
> in structuredContent passes through to the agent. MCP does not distinguish
> this from any other successful structured content — the `{"ok": False}`
> convention is meaningless at the MCP layer. This is already covered by
> `test_mcp_integration.py` which asserts structured content passthrough
> (line 37: `has_entries(echo="hello")`, line 56: direct equality check).
> Delete the redundant test and its `error_payload_server` fixture.

```
      92:     x: int
      93:
      94:
>>>   95: async def test_app_level_error_payload_surfaced_in_structured_content(
>>>   96:     compositor, compositor_client, error_payload_server, recording_handler
>>>   97: ) -> None:
>>>   98:     """Test that application-level error payloads are surfaced in structuredContent.
>>>   99:
>>>  100:     Note: This tests the {"ok": False, "error": "..."} pattern in structuredContent,
>>>  101:     NOT the MCP-level isError flag. For MCP-level error testing, see test_tool_error_continues_turn.
>>>  102:     """
>>>  103:     mounted = await compositor.mount_inproc(MCPMountPrefix("editor"), error_payload_server)
>>>  104:
>>>  105:     @DecoratorMock.mock()
>>>  106:     def mock(m: DecoratorMock):
>>>  107:         yield
>>>  108:         yield m.tool_call(build_mcp_function(mounted.prefix, FAIL_TOOL_NAME), FailInput(x=1))
>>>  109:         yield m.assistant_text("done")
>>>  110:
>>>  111:     agent = await Agent.create(
>>>  112:         mcp_client=compositor_client,
>>>  113:         client=mock,
>>>  114:         handlers=[FinishOnTextMessageHandler(), recording_handler],
>>>  115:         tool_policy=AllowAnyToolOrTextMessage(),
>>>  116:     )
>>>  117:     agent.process_message(UserMessage.text("fail"))
>>>  118:     await agent.run()
>>>  119:
>>>  120:     assert_function_call_output_structured(recording_handler.records, has_entries(ok=False, error="boom"))
     121:
     122:
     123: # --- Parallel tool call tests ---
```

### `str-keyed-state-map.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/str-keyed-state-map.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/wt/client/view_formatter.py#L199-L206)

File: `wt/client/view_formatter.py` (L199-206)

> `state_map` (view_formatter.py:199-205) is keyed by string literals
> (`"running"`, `"failed"`, etc.) when `gitstatusd_state` is a
> `GitstatusdState` StrEnum. The lookup coerces via `or ""` to avoid None.
> Key by `GitstatusdState` enum values instead.
>
> Additionally, the map is a trivial identity mapping — every key maps to
> itself. The entire map and lookup can be replaced with
> `status.gitstatusd_state or ""` (or `.value` if an explicit str is needed).

```
     196:             pr_link = self._get_pr_link_column(status)
     197:             pr_status = self._get_pr_status_column(status)
     198:             pr_changes = self._get_pr_changes_column(status)
>>>  199:             state_map = {
>>>  200:                 "running": "running",
>>>  201:                 "restarting": "restarting",
>>>  202:                 "failed": "failed",
>>>  203:                 "stopped": "stopped",
>>>  204:                 "starting": "starting",
>>>  205:             }
>>>  206:             state = state_map.get(status.gitstatusd_state or "", "")
     207:             if pr_link:
     208:                 pr_parts = [pr_link]
     209:                 if pr_status:
```

### `str-keyed-status-map.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/str-keyed-status-map.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/wt/client/view_formatter.py#L14-L291)

File: `wt/client/view_formatter.py` (L14-20, L61-80, L286-291)

> `PR_STATUS_DISPLAY_MAP` (view_formatter.py:14-20) is keyed by
> `PRStatus.*.display_text` strings rather than `PRStatus` enum values.
> `get_pr_status_text` (lines 61-80) manually resolves `PRState` + mergeability
>
> - merged_at into a display string, duplicating the `PRState → PRStatus`
>   resolution that should produce a `PRStatus` enum value. The lookup site
>   (line 287) then compares this string output against display-text keys.
>
> The entire chain is string-based when it should be enum-based:
> `get_pr_status_text` should return `PRStatus` (not `str`), the map should
> be keyed by `PRStatus`, and display formatting should happen at the end.
> As-is, any change to `display_text` silently breaks the map lookup.

```
      11: from ..shared.protocol import PRInfo, PRInfoError, PRInfoOk, StatusResult
      12:
      13: # PR status display mapping centralized via PRStatus.display_text
>>>   14: PR_STATUS_DISPLAY_MAP = {
>>>   15:     PRStatus.MERGED.display_text: ("✅", "already merged"),
>>>   16:     PRStatus.CLOSED.display_text: ("❌", "closed"),
>>>   17:     PRStatus.OPEN_MERGEABLE.display_text: ("🟢", "can merge"),
>>>   18:     PRStatus.OPEN_CONFLICTING.display_text: ("🔴", "has conflict"),
>>>   19:     PRStatus.OPEN_UNKNOWN.display_text: ("🟡", "open"),
>>>   20: }
      21:
      22:
      23: def format_sync_status(ahead: int, behind: int, *, compact: bool = False) -> str:
   ...
      58:             return "unknown"
      59:         return "mergeable" if mergeable else "conflicting"
      60:
>>>   61:     def get_pr_status_text(
>>>   62:         self,
>>>   63:         pr_state: PRState,
>>>   64:         mergeability: Literal["mergeable", "conflicting", "unknown"],
>>>   65:         is_draft: bool = False,
>>>   66:         merged_at: str | None = None,
>>>   67:     ) -> str:
>>>   68:         # Show draft status first if it's a draft
>>>   69:         if is_draft:
>>>   70:             return "draft"
>>>   71:
>>>   72:         # Distinguish between merged and closed based on merged_at
>>>   73:         if pr_state == PRState.CLOSED:
>>>   74:             return "merged" if merged_at else "closed"
>>>   75:         if pr_state == PRState.OPEN:
>>>   76:             if mergeability == "unknown":
>>>   77:                 return PRStatus.OPEN_UNKNOWN.display_text
>>>   78:             if mergeability == "mergeable":
>>>   79:                 return PRStatus.OPEN_MERGEABLE.display_text
>>>   80:             return PRStatus.OPEN_CONFLICTING.display_text
      81:         return str(pr_state.value).lower()
      82:
      83:     def format_status_row(self, name: str, status: StatusResult, pr_info: PRInfo | None, name_width: int = 22) -> str:
   ...
     283:             )
     284:
     285:             # Format detailed PR status
>>>  286:             status_text = self.get_pr_status_text(pr_state, self._mergeability_label(d.mergeable), d.draft, d.merged_at)
>>>  287:             if status_text in PR_STATUS_DISPLAY_MAP:
>>>  288:                 icon, message = PR_STATUS_DISPLAY_MAP[status_text]
>>>  289:                 click.echo(f"{icon} Status: This PR {message}")
>>>  290:             else:
>>>  291:                 click.echo(f"Status: {status_text}")
     292:
     293:     def render_worktree_removal_confirmation(self, name: str, worktree_path: Path) -> None:
     294:         click.echo(f"⚠️  About to permanently remove worktree '{name}' at {worktree_path}")
```

### `trivial-wrapper.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/trivial-wrapper.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/agent_core/conftest.py#L24-L27)

File: `agent_core/conftest.py` (L24-27)

> `text_content` fixture (conftest.py:25) returns a lambda that calls
> `mcp_types.TextContent(type="text", text=text)`. This is a trivial wrapper
> — callers should construct `TextContent` directly.

```
      21: ]
      22:
      23:
>>>   24: @pytest.fixture
>>>   25: def text_content():
>>>   26:     """Helper to create MCP TextContent blocks."""
>>>   27:     return lambda text: mcp_types.TextContent(type="text", text=text)
      28:
      29:
      30: @pytest.fixture
```

### `walrus.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/walrus.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/wt/server/handlers/status_handler.py#L54-L56)

File: `wt/server/handlers/status_handler.py` (L54-56)

> Assign-then-check patterns that should use walrus operator (:=).
>
> **Note:** `cached = ahead_behind_data.get(branch)` then `if cached:`

```
      51: ) -> tuple[int | None, int | None]:
      52:     if not branch:
      53:         return (None, None)
>>>   54:     cached = ahead_behind_data.get(branch)
>>>   55:     if cached:
>>>   56:         return (cached.ahead, cached.behind)
      57:     return (None, None)
      58:
      59:
```

### `walrus.yaml` / `occ-1`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/issues/walrus.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-17-00/code/wt/client/view_formatter.py#L197-L212)

File: `wt/client/view_formatter.py` (L197-198, L209-212)

> Assign-then-check patterns that should use walrus operator (:=).
>
> **Note:** `pr_status` and `pr_changes` assigned then checked with `if`

```
     194:         for name, status in sorted_items:
     195:             pr_info = ""
     196:             pr_link = self._get_pr_link_column(status)
>>>  197:             pr_status = self._get_pr_status_column(status)
>>>  198:             pr_changes = self._get_pr_changes_column(status)
     199:             state_map = {
     200:                 "running": "running",
     201:                 "restarting": "restarting",
   ...
     206:             state = state_map.get(status.gitstatusd_state or "", "")
     207:             if pr_link:
     208:                 pr_parts = [pr_link]
>>>  209:                 if pr_status:
>>>  210:                     pr_parts.append(pr_status)
>>>  211:                 if pr_changes:
>>>  212:                     pr_parts.append(pr_changes)
     213:                 pr_info = " ".join(pr_parts)
     214:
     215:             table_data.append(
```

## ducktape/2026-01-29-00 (22)

### `dead-code.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/dead-code.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/mcp_infra/calltool.py#L7-L19)

File: `mcp_infra/calltool.py` (L7-19)

> Unused code that is never reached. Remove it.
>
> **Note:** `extract_structured_content` never imported or called

```
       4: from pydantic import TypeAdapter
       5:
       6:
>>>    7: def extract_structured_content[T](result: mcp_types.CallToolResult, output_type: type[T]) -> T:
>>>    8:     """Extract and validate structured content from a tool result.
>>>    9:
>>>   10:     Raises ValueError if result is an error or lacks structured content.
>>>   11:     """
>>>   12:     if result.isError:
>>>   13:         raise ValueError(f"Cannot extract from error result: {result}")
>>>   14:
>>>   15:     sc = result.structuredContent
>>>   16:     if sc is None:
>>>   17:         raise ValueError(f"CallToolResult missing structured content: {result}")
>>>   18:
>>>   19:     return TypeAdapter(output_type).validate_python(sc)
```

### `dead-code.yaml` / `occ-1`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/dead-code.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/agent_server/mcp/matrix/control.py#L19-L30)

File: `agent_server/mcp/matrix/control.py` (L19-30)

> Unused code that is never reached. Remove it.
>
> **Note:** `make_matrix_control_server` never imported — matrix_bot.py inlines identical logic

```
      16: from mcp_infra.enhanced.server import EnhancedFastMCP
      17:
      18:
>>>   19: def make_matrix_control_server(bus: ServerBus) -> EnhancedFastMCP:
>>>   20:     mcp = EnhancedFastMCP(
>>>   21:         "Matrix Control Server", instructions=("Matrix control: yield-only control to signal end of turn.")
>>>   22:     )
>>>   23:
>>>   24:     @mcp.flat_model()
>>>   25:     def do_yield() -> UiEndTurn:
>>>   26:         """End the current turn. The runner will wake you on new DMs."""
>>>   27:         bus.push_end_turn()
>>>   28:         return UiEndTurn()
>>>   29:
>>>   30:     return mcp
```

### `dead-code.yaml` / `occ-10`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/dead-code.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/props/cli/cmd_stats.py#L63-L64)

File: `props/cli/cmd_stats.py` (L63-64)

> Unused code that is never reached. Remove it.
>
> **Note:** `fmt_hash` never called

```
      60:
      61: def fmt_hash(hash_value: str | None) -> str:
      62:     return short_sha(hash_value) if hash_value else "—"
>>>   63:
>>>   64:
      65: # Common dimension columns (used by prompt and example stats)
      66: _DIMENSION_COLUMNS: list[ColumnDef[Any, Any]] = [
      67:     ColumnDef("Split", lambda r: r.split, width=6),
```

### `dead-code.yaml` / `occ-12`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/dead-code.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/props/cli/__main__.py#L125-L133)

File: `props/cli/__main__.py` (L125-133)

> Unused code that is never reached. Remove it.
>
> **Note:** `MetricsRow` dataclass never instantiated

```
     122:         typer.echo(agent_run.type_config.model_dump_json(indent=2))
     123:
     124:
>>>  125: @dataclass
>>>  126: class MetricsRow:
>>>  127:     iteration: int
>>>  128:     mean_recall: float
>>>  129:     tp: int
>>>  130:     fp: int
>>>  131:     fn: int
>>>  132:     unknown: int
>>>  133:     dir: str
     134:
     135:
     136: def read_embedded_paths(paths: list[Path]) -> str:
```

### `dead-code.yaml` / `occ-14`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/dead-code.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/props/core/models/types.py#L41-L56)

File: `props/core/models/types.py` (L41-56)

> Unused code that is never reached. Remove it.
>
> **Note:** `classify_path` never called

```
      38:     OTHER = "other"
      39:
      40:
>>>   41: def classify_path(p: Path) -> FileType:
>>>   42:     """Classify a path's file type.
>>>   43:
>>>   44:     Must check in this order:
>>>   45:     1. is_symlink() - symlinks report True for is_file()/is_dir()
>>>   46:     2. is_dir()
>>>   47:     3. is_file()
>>>   48:     4. else OTHER
>>>   49:     """
>>>   50:     if p.is_symlink():
>>>   51:         return FileType.SYMLINK
>>>   52:     if p.is_dir():
>>>   53:         return FileType.DIRECTORY
>>>   54:     if p.is_file():
>>>   55:         return FileType.REGULAR
>>>   56:     return FileType.OTHER
      57:
      58:
      59: def _validate_specimen_relative_path(v: Any, handler: Any, info: ValidationInfo) -> Path:
```

### `dead-code.yaml` / `occ-15`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/dead-code.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/props/core/models/true_positive.py#L211-L222)

File: `props/core/models/true_positive.py` (L211-222)

> Unused code that is never reached. Remove it.
>
> **Note:** `IssueCore` never used in Python code

```
     208: # BaseIssueID imported from ids module (validates no colons)
     209:
     210:
>>>  211: class IssueCore(BaseModel):
>>>  212:     """True positive metadata without occurrences.
>>>  213:
>>>  214:     Minimal header describing a logical problem.
>>>  215:     When sending or storing per-location data separately, pair an IssueCore with
>>>  216:     one or more Occurrence objects rather than repeating metadata.
>>>  217:     """
>>>  218:
>>>  219:     id: BaseIssueID
>>>  220:     rationale: Rationale
>>>  221:
>>>  222:     model_config = ConfigDict(extra="forbid")
```

### `dead-code.yaml` / `occ-16`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/dead-code.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/props/core/models/true_positive.py#L192-L204)

File: `props/core/models/true_positive.py` (L192-204)

> Unused code that is never reached. Remove it.
>
> **Note:** `SnapshotIssuesLoadError` never raised or caught

```
     189:     model_config = ConfigDict(extra="forbid")
     190:
     191:
>>>  192: class SnapshotIssuesLoadError(Exception):
>>>  193:     """Raised when per-issue Jsonnet evaluation/validation yields any errors in strict mode.
>>>  194:
>>>  195:     Carries a list of human-readable error lines. __str__ joins them with newlines
>>>  196:     so pytest and CLIs surface a readable summary.
>>>  197:     """
>>>  198:
>>>  199:     def __init__(self, errors: list[str]):
>>>  200:         self.errors = errors
>>>  201:         super().__init__(str(self))
>>>  202:
>>>  203:     def __str__(self) -> str:  # pragma: no cover - exercised via message rendering
>>>  204:         return "Snapshot issue loading errors:\n" + "\n".join(self.errors)
     205:
     206:
     207: # Strongly-typed identifiers with validation
```

### `dead-code.yaml` / `occ-17`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/dead-code.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/props/db/sync/sync.py#L723-L732)

File: `props/db/sync/sync.py` (L723-732)

> Unused code that is never reached. Remove it.
>
> **Note:** `sync_model_metadata` wrapper never called — `sync_model_metadata_with_session` used directly

```
     720:         return f"{self.total} models (+{self.added}, ~{self.updated}, -{self.deleted})"
     721:
     722:
>>>  723: def sync_model_metadata() -> ModelMetadataSyncStats:
>>>  724:     """Sync model_metadata table from MODEL_METADATA source.
>>>  725:
>>>  726:     Opens its own session internally (legacy interface for backward compatibility).
>>>  727:
>>>  728:     Returns:
>>>  729:         Statistics about what changed
>>>  730:     """
>>>  731:     with get_session() as session:
>>>  732:         return sync_model_metadata_with_session(session)
     733:
     734:
     735: def sync_model_metadata_with_session(session: Session) -> ModelMetadataSyncStats:
```

### `dead-code.yaml` / `occ-18`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/dead-code.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/props/db/session.py#L143-L145)

File: `props/db/session.py` (L143-145)

> Unused code that is never reached. Remove it.
>
> **Note:** `is_db_initialized` never called

```
     140:             _engine = None
     141:
     142:
>>>  143: def is_db_initialized() -> bool:
>>>  144:     """Check if database connection is already established."""
>>>  145:     return _engine is not None
     146:
     147:
     148: def init_db(config: DatabaseConfig | None = None) -> None:
```

### `dead-code.yaml` / `occ-19`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/dead-code.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/props/db/session.py#L173-L185)

File: `props/db/session.py` (L173-185)

> Unused code that is never reached. Remove it.
>
> **Note:** `check_connection` never called

```
     170:     _get_engine(config)
     171:
     172:
>>>  173: def check_connection(timeout_secs: int = 2) -> None:
>>>  174:     """Validate database connection (fail fast if DB not reachable).
>>>  175:
>>>  176:     Args:
>>>  177:         timeout_secs: Connection timeout in seconds (default: 2)
>>>  178:
>>>  179:     Raises:
>>>  180:         RuntimeError: If database not initialized (call init_db() first)
>>>  181:         sqlalchemy.exc.OperationalError: If cannot connect to database within timeout
>>>  182:     """
>>>  183:     if _engine is None:
>>>  184:         raise RuntimeError("Database not initialized. Call init_db() first.")
>>>  185:     _check_connection_internal(timeout_secs)
     186:
     187:
     188: def recreate_database() -> None:
```

### `dead-code.yaml` / `occ-5`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/dead-code.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/props/grader/daemon.py#L1-L136)

File: `props/grader/daemon.py` (L1-136)

> Unused code that is never reached. Remove it.
>
> **Note:** `GraderDaemonScaffold` — entire module never imported

```
>>>    1: """Grader daemon scaffold for persistent snapshot grading.
>>>    2:
>>>    3: The daemon is a k8s controller-style reconciliation loop:
>>>    4: - Goal: make grading_pending empty for its snapshot
>>>    5: - When drift exists → grade; when empty → sleep until woken by pg_notify
>>>    6: - Context exhaustion → restart with fresh agent, query remaining drift
>>>    7: """
>>>    8:
>>>    9: from __future__ import annotations
>>>   10:
>>>   11: import asyncio
>>>   12: import logging
>>>   13: from typing import Any
>>>   14:
>>>   15: import asyncpg
>>>   16: from asyncpg.pool import PoolConnectionProxy
>>>   17:
>>>   18: from props.core.ids import SnapshotSlug
>>>   19: from props.db.config import DatabaseConfig
>>>   20: from props.grader.drift_handler import GraderDriftHandler, check_grading_pending
>>>   21: from props.grader.notifications import GRADING_PENDING_CHANNEL, GradingPendingNotification
>>>   22:
>>>   23: logger = logging.getLogger(__name__)
>>>   24:
>>>   25:
>>>   26: class GraderDaemonScaffold:
>>>   27:     """Scaffold that manages the grader daemon lifecycle.
>>>   28:
>>>   29:     Responsibilities:
>>>   30:     - Background pg_listen task for notifications
>>>   31:     - Wake/sleep coordination via asyncio.Event
>>>   32:     - Agent run loop with restart on context exhaustion
>>>   33:     - Notification queue management
>>>   34:     """
>>>   35:
>>>   36:     def __init__(self, snapshot_slug: SnapshotSlug, db_config: DatabaseConfig):
>>>   37:         self._snapshot_slug = snapshot_slug
>>>   38:         self._db_config = db_config
>>>   39:         self._notification_queue: list[GradingPendingNotification] = []
>>>   40:         self._wake_event = asyncio.Event()
>>>   41:         self._listener_task: asyncio.Task | None = None
>>>   42:         self._listener_conn: asyncpg.Connection | None = None
>>>   43:         self._shutdown = False
>>>   44:
>>>   45:     @property
>>>   46:     def snapshot_slug(self) -> SnapshotSlug:
>>>   47:         return self._snapshot_slug
>>>   48:
>>>   49:     @property
>>>   50:     def notification_queue(self) -> list[GradingPendingNotification]:
>>>   51:         """Shared queue for handler to drain."""
>>>   52:         return self._notification_queue
>>>   53:
>>>   54:     @property
>>>   55:     def wake_event(self) -> asyncio.Event:
>>>   56:         """Event set when notifications arrive."""
>>>   57:         return self._wake_event
>>>   58:
>>>   59:     def create_drift_handler(self) -> GraderDriftHandler:
>>>   60:         """Create drift handler with shared queue and event."""
>>>   61:         return GraderDriftHandler(
>>>   62:             snapshot_slug=self._snapshot_slug, notification_queue=self._notification_queue, wake_event=self._wake_event
>>>   63:         )
>>>   64:
>>>   65:     def _notification_callback(
>>>   66:         self, connection: asyncpg.Connection[Any] | PoolConnectionProxy[Any], pid: int, channel: str, payload: object
>>>   67:     ) -> None:
>>>   68:         """Handle incoming pg_notify notifications."""
>>>   69:         if not isinstance(payload, str):
>>>   70:             raise TypeError(f"Expected string payload, got {type(payload)}")
>>>   71:
>>>   72:         notification = GradingPendingNotification.model_validate_json(payload)
>>>   73:
>>>   74:         if notification.snapshot_slug != self._snapshot_slug:
>>>   75:             return  # Not for us
>>>   76:
>>>   77:         logger.debug(f"Notification for {self._snapshot_slug}: {notification.operation} {notification.item.table}")
>>>   78:         self._notification_queue.append(notification)
>>>   79:         self._wake_event.set()
>>>   80:
>>>   81:     async def _start_listener(self) -> None:
>>>   82:         """Start background pg_listen task."""
>>>   83:         self._listener_conn = await asyncpg.connect(self._db_config.admin.url())
>>>   84:         await self._listener_conn.add_listener(GRADING_PENDING_CHANNEL, self._notification_callback)
>>>   85:         logger.info(f"Listening on channel '{GRADING_PENDING_CHANNEL}' for {self._snapshot_slug}")
>>>   86:
>>>   87:     async def _stop_listener(self) -> None:
>>>   88:         """Stop background listener."""
>>>   89:         if self._listener_conn:
>>>   90:             try:
>>>   91:                 await self._listener_conn.remove_listener(GRADING_PENDING_CHANNEL, self._notification_callback)
>>>   92:                 await self._listener_conn.close()
>>>   93:             except Exception as e:
>>>   94:                 logger.warning(f"Error closing listener connection: {e}")
>>>   95:             self._listener_conn = None
>>>   96:
>>>   97:     async def wait_for_drift_or_notification(self) -> list[GradingPendingNotification]:
>>>   98:         """Wait until there's drift or a notification arrives.
>>>   99:
>>>  100:         Returns accumulated notifications (may be empty if drift detected on check).
>>>  101:         """
>>>  102:         # First check if there's already drift
>>>  103:         if check_grading_pending(self._snapshot_slug):
>>>  104:             # There's work to do, don't wait
>>>  105:             notifs = list(self._notification_queue)
>>>  106:             self._notification_queue.clear()
>>>  107:             return notifs
>>>  108:
>>>  109:         # No drift, wait for notification
>>>  110:         logger.info(f"No drift for {self._snapshot_slug}, waiting for notification...")
>>>  111:         self._wake_event.clear()
>>>  112:         await self._wake_event.wait()
>>>  113:
>>>  114:         # Drain queue
>>>  115:         notifs = list(self._notification_queue)
>>>  116:         self._notification_queue.clear()
>>>  117:         return notifs
>>>  118:
>>>  119:     async def __aenter__(self) -> GraderDaemonScaffold:
>>>  120:         """Start listener on context entry."""
>>>  121:         await self._start_listener()
>>>  122:         return self
>>>  123:
>>>  124:     async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
>>>  125:         """Stop listener on context exit."""
>>>  126:         self._shutdown = True
>>>  127:         await self._stop_listener()
>>>  128:
>>>  129:     def shutdown(self) -> None:
>>>  130:         """Signal shutdown (can be called from another task)."""
>>>  131:         self._shutdown = True
>>>  132:         self._wake_event.set()  # Wake up if sleeping
>>>  133:
>>>  134:     @property
>>>  135:     def is_shutdown(self) -> bool:
>>>  136:         return self._shutdown
```

### `dead-code.yaml` / `occ-7`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/dead-code.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/props/core/runs_context.py#L17-L112)

File: `props/core/runs_context.py` (L17-19, L73-112)

> Unused code that is never reached. Remove it.
>
> **Note:** `pkg_dir` and `RunsContext` — never imported or instantiated

```
      14: logger = logging.getLogger(__name__)
      15:
      16:
>>>   17: def pkg_dir() -> Path:
>>>   18:     """Root directory of this package resources."""
>>>   19:     return Path(__file__).parent
      20:
      21:
      22: def specimens_definitions_root() -> Path:
   ...
      70:     return dt.strftime("%Y%m%d_%H%M%S")
      71:
      72:
>>>   73: class RunsContext:
>>>   74:     """Context object for runs directory path derivation.
>>>   75:
>>>   76:     Injected at CLI/entry point level. All path construction goes through this object.
>>>   77:     No code should independently compute runs paths or use hardcoded path tokens.
>>>   78:     """
>>>   79:
>>>   80:     def __init__(self, base_dir: Path):
>>>   81:         """Initialize runs context.
>>>   82:
>>>   83:         Args:
>>>   84:             base_dir: Base runs directory (e.g., pkg_dir() / "runs")
>>>   85:         """
>>>   86:         self.base_dir = base_dir
>>>   87:
>>>   88:     @classmethod
>>>   89:     def from_pkg_dir(cls) -> RunsContext:
>>>   90:         """Create RunsContext from package directory (default location).
>>>   91:
>>>   92:         Returns:
>>>   93:             RunsContext for pkg_dir() / "runs"
>>>   94:         """
>>>   95:         return cls(pkg_dir() / "runs")
>>>   96:
>>>   97:     def issue_eval_dir(self, identifier: str, timestamp: str | None = None) -> Path:
>>>   98:         """Get output directory for issue evaluation runs (lint_issue harness).
>>>   99:
>>>  100:         Args:
>>>  101:             identifier: Identifier for the eval (e.g., snapshot_issue_id or "all")
>>>  102:             timestamp: Optional timestamp string (defaults to creating new one)
>>>  103:
>>>  104:         Returns:
>>>  105:             Path to eval output directory (created if it doesn't exist)
>>>  106:             Structure: runs/evals/{identifier}_{timestamp}/
>>>  107:         """
>>>  108:         if timestamp is None:
>>>  109:             timestamp = format_timestamp_session()
>>>  110:         path = self.base_dir / "evals" / f"{identifier}_{timestamp}"
>>>  111:         path.mkdir(parents=True, exist_ok=True)
>>>  112:         return path
```

### `dead-code.yaml` / `occ-9`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/dead-code.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/props/critic_dev/optimize/budget_handler.py#L1-L118)

File: `props/critic_dev/optimize/budget_handler.py` (L1-118)

> Unused code that is never reached. Remove it.
>
> **Note:** `BudgetState` and `BudgetEnforcementHandler` — entire module never imported

```
>>>    1: """Budget enforcement handler for prompt optimization runs.
>>>    2:
>>>    3: Monitors cumulative costs across critic/grader runs and enforces budget limits by:
>>>    4: 1. Checking if budget is exhausted after each tool result
>>>    5: 2. When budget reached: inject final system message and switch to text-only mode
>>>    6: 3. Agent produces summary report (detected via on_assistant_text_event)
>>>    7: 4. Abort on next sample
>>>    8: """
>>>    9:
>>>   10: from __future__ import annotations
>>>   11:
>>>   12: import logging
>>>   13: from enum import StrEnum
>>>   14: from typing import TYPE_CHECKING
>>>   15: from uuid import UUID
>>>   16:
>>>   17: from sqlalchemy.orm import Session
>>>   18:
>>>   19: from agent_core.events import AssistantText
>>>   20: from agent_core.handler import BaseHandler
>>>   21: from agent_core.loop_control import Abort, ForbidAllTools, InjectItems, LoopDecision, NoAction
>>>   22: from openai_utils.model import UserMessage
>>>   23: from props.db import query_builders as qb
>>>   24: from props.db.session import get_session
>>>   25:
>>>   26: if TYPE_CHECKING:
>>>   27:     from agent_core.agent import Agent
>>>   28:
>>>   29: logger = logging.getLogger(__name__)
>>>   30:
>>>   31:
>>>   32: class BudgetState(StrEnum):
>>>   33:     """Budget enforcement state machine states."""
>>>   34:
>>>   35:     MONITORING = "monitoring"
>>>   36:     SUMMARY_REQUESTED = "summary_requested"
>>>   37:     SUMMARY_PRODUCED = "summary_produced"
>>>   38:
>>>   39:
>>>   40: class BudgetEnforcementHandler(BaseHandler):
>>>   41:     """Enforce budget limits for prompt optimization runs.
>>>   42:
>>>   43:     Tracks cumulative costs across all critic/grader runs linked to a PO run ID.
>>>   44:     When budget is reached:
>>>   45:     1. Inject system message requesting final summary report
>>>   46:     2. Switch agent to text-only mode (ForbidAllTools)
>>>   47:     3. Allow agent one final turn to produce report
>>>   48:     4. Abort on next sample attempt
>>>   49:
>>>   50:     State machine:
>>>   51:     - MONITORING: normal operation, checking budget before each sample
>>>   52:     - SUMMARY_REQUESTED: budget exceeded, injected summary request, waiting for final text response
>>>   53:     - SUMMARY_PRODUCED: got final response, ready to abort
>>>   54:     """
>>>   55:
>>>   56:     def __init__(
>>>   57:         self,
>>>   58:         *,
>>>   59:         optimizer_run_id: UUID,
>>>   60:         budget_limit: float,  # USD
>>>   61:         agent: Agent,
>>>   62:     ) -> None:
>>>   63:         self._optimizer_run_id = optimizer_run_id
>>>   64:         self._budget_limit = budget_limit
>>>   65:         self._agent = agent
>>>   66:         self._state = BudgetState.MONITORING
>>>   67:
>>>   68:     def _query_total_cost(self, session: Session) -> float:
>>>   69:         result = session.execute(qb.po_run_costs(self._optimizer_run_id)).fetchall()
>>>   70:         return sum((row.cost_usd for row in result if row.cost_usd is not None), start=0.0)
>>>   71:
>>>   72:     def on_assistant_text_event(self, evt: AssistantText) -> None:
>>>   73:         if self._state == BudgetState.SUMMARY_REQUESTED:
>>>   74:             self._state = BudgetState.SUMMARY_PRODUCED
>>>   75:             logger.info(f"PO run {self._optimizer_run_id}: Summary report produced, will abort on next sample")
>>>   76:
>>>   77:     def on_before_sample(self) -> LoopDecision:
>>>   78:         """Enforce budget limits before each sampling step.
>>>   79:
>>>   80:         State transitions:
>>>   81:         1. SUMMARY_PRODUCED → Abort
>>>   82:         2. MONITORING with budget exceeded → inject summary request, transition to SUMMARY_REQUESTED
>>>   83:         3. SUMMARY_REQUESTED → NoAction (waiting for response)
>>>   84:         4. MONITORING with budget OK → NoAction
>>>   85:         """
>>>   86:         if self._state == BudgetState.SUMMARY_PRODUCED:
>>>   87:             logger.info(f"PO run {self._optimizer_run_id}: Aborting after summary")
>>>   88:             return Abort()
>>>   89:
>>>   90:         if self._state == BudgetState.MONITORING:
>>>   91:             with get_session() as session:
>>>   92:                 cumulative_cost = self._query_total_cost(session)
>>>   93:
>>>   94:             if cumulative_cost >= self._budget_limit:
>>>   95:                 logger.info(
>>>   96:                     f"PO run {self._optimizer_run_id}: Budget exhausted (${cumulative_cost:.4f} >= ${self._budget_limit:.2f})"
>>>   97:                 )
>>>   98:                 self._state = BudgetState.SUMMARY_REQUESTED
>>>   99:                 self._agent._tool_policy = ForbidAllTools()
>>>  100:                 logger.info(f"PO run {self._optimizer_run_id}: Switched to text-only mode (ForbidAllTools)")
>>>  101:
>>>  102:                 summary_request = UserMessage.text(
>>>  103:                     f"""\
>>>  104: Your budget of ${self._budget_limit:.2f} has been exceeded.
>>>  105: Tool calls are now disabled. Produce a final summary report with:
>>>  106:
>>>  107: 1. **Best prompt found**: prompt SHA256 and key insights
>>>  108: 2. **Performance summary**: best recall achieved on valid split
>>>  109: 3. **Key learnings**: what worked, what didn't, patterns discovered
>>>  110: 4. **Recommendations**: next steps for further optimization
>>>  111:
>>>  112: Make this your final response - the session will end after this message.
>>>  113: """
>>>  114:                 )
>>>  115:
>>>  116:                 return InjectItems(items=[summary_request])
>>>  117:
>>>  118:         return NoAction()
```

### `metadata-cache-drop.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/metadata-cache-drop.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/gmail_archiver/inbox.py#L77-L97)

File: `gmail_archiver/inbox.py` (L77, L94-97)

> `GmailInbox.fetch_messages_metadata` silently drops messages that are in
> `_full_cache` but not `_metadata_cache`. Line 77 skips re-fetching IDs
> already in `_full_cache` (correct, since full data is a superset of
> metadata). But lines 94-97 then fail to include those IDs in the result:
> the `elif mid in self._full_cache` branch executes `pass` instead of
> appending to `result`. Any message previously fetched via `fetch_messages`
> (which populates `_full_cache`) will be silently omitted from subsequent
> `fetch_messages_metadata` calls.

```
      74:             return []
      75:
      76:         # Check both caches - if in full cache, we can derive metadata
>>>   77:         uncached_ids = [mid for mid in message_ids if mid not in self._metadata_cache and mid not in self._full_cache]
      78:
      79:         if self.show_progress and uncached_ids:
      80:             self.console.print(
   ...
      91:         for mid in message_ids:
      92:             if mid in self._metadata_cache:
      93:                 result.append(self._metadata_cache[mid])
>>>   94:             elif mid in self._full_cache:
>>>   95:                 # Derive metadata from full email - create GmailMessageWithHeaders-like object
>>>   96:                 # For now, just skip - the planner should have it from metadata cache
>>>   97:                 pass
      98:         return result
      99:
     100:     def get_message(self, message_id: str) -> Email | GmailMessageWithHeaders:
```

### `misplaced-tests.yaml` / `occ-2`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/misplaced-tests.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/agent_server/test_mcp_resources_flow.py#L1-L62)

File: `agent_server/test_mcp_resources_flow.py` (L1-62)

> These test files live in agent_server/ but primarily test code in mcp_infra/
> (exec servers, compositor rendering, resources server). Tests for package X
> should not live in package Y just because Y depends on X. They belong under
> mcp_infra/ alongside the code they exercise.
>
> **Note:** Tests mcp_infra/compositor/resources_server.py

```
>>>    1: from __future__ import annotations
>>>    2:
>>>    3: import pytest
>>>    4: import pytest_bazel
>>>    5: from hamcrest import assert_that, has_item, instance_of
>>>    6:
>>>    7: from agent_core.agent import Agent
>>>    8: from agent_core.events import ToolCall, ToolCallOutput
>>>    9: from agent_core.handler import FinishOnTextMessageHandler
>>>   10: from agent_core.loop_control import RequireAnyTool
>>>   11: from agent_core.mcp_provider import MCPToolProvider
>>>   12: from agent_core_testing.responses import DecoratorMock
>>>   13: from mcp_infra.compositor.resources_server import ResourcesReadArgs
>>>   14: from mcp_infra.display.event_renderer import DisplayEventsHandler
>>>   15: from mcp_infra.prefix import MCPMountPrefix
>>>   16: from openai_utils.model import FunctionCallItem, FunctionCallOutputItem, UserMessage
>>>   17:
>>>   18:
>>>   19: @pytest.mark.requires_docker
>>>   20: async def test_model_reads_container_info_with_stubbed_openai(
>>>   21:     reasoning_model, docker_exec_server_py312slim, compositor, compositor_client, recording_handler
>>>   22: ) -> None:
>>>   23:     """Test model reading container info resources without policy gateway."""
>>>   24:     # Mount runtime server and capture Mounted object
>>>   25:     mounted_runtime = await compositor.mount_inproc(MCPMountPrefix("runtime"), docker_exec_server_py312slim)
>>>   26:
>>>   27:     # Get container info URI from server instance (convert to string)
>>>   28:     container_info_uri = str(docker_exec_server_py312slim.container_info_resource.uri)
>>>   29:
>>>   30:     @DecoratorMock.mock()
>>>   31:     def mock(m: DecoratorMock):
>>>   32:         # First turn: receive request, return tool call
>>>   33:         _ = yield
>>>   34:         tool_call = m.mcp_tool_call(
>>>   35:             MCPMountPrefix("resources"),
>>>   36:             "read",
>>>   37:             ResourcesReadArgs(server=mounted_runtime.prefix, uri=container_info_uri, start_offset=0, max_bytes=1024),
>>>   38:         )
>>>   39:         # Second turn: receive request with tool output, return final text
>>>   40:         req = yield tool_call
>>>   41:         # Verify stateless replay: second call must include function_call and function_call_output
>>>   42:         assert isinstance(req.input, list)
>>>   43:         assert_that(req.input, has_item(instance_of(FunctionCallItem)))
>>>   44:         assert_that(req.input, has_item(instance_of(FunctionCallOutputItem)))
>>>   45:         yield m.assistant_text("ok")
>>>   46:
>>>   47:     agent = await Agent.create(
>>>   48:         tool_provider=MCPToolProvider(compositor_client),
>>>   49:         client=mock,
>>>   50:         handlers=[FinishOnTextMessageHandler(), DisplayEventsHandler(), recording_handler],
>>>   51:         tool_policy=RequireAnyTool(),
>>>   52:     )
>>>   53:     agent.process_message(UserMessage.text("read container info"))
>>>   54:
>>>   55:     await agent.run()
>>>   56:     types = [e.type for e in recording_handler.records if isinstance(e, ToolCall | ToolCallOutput)]
>>>   57:     assert "tool_call" in types
>>>   58:     assert "function_call_output" in types
>>>   59:
>>>   60:
>>>   61: if __name__ == "__main__":
>>>   62:     pytest_bazel.main()
```

### `misplaced-tests.yaml` / `occ-3`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/misplaced-tests.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/agent_server/test_rendering_instructions.py#L1-L41)

File: `agent_server/test_rendering_instructions.py` (L1-41)

> These test files live in agent_server/ but primarily test code in mcp_infra/
> (exec servers, compositor rendering, resources server). Tests for package X
> should not live in package Y just because Y depends on X. They belong under
> mcp_infra/ alongside the code they exercise.
>
> **Note:** Tests mcp_infra/compositor/rendering.py

```
>>>    1: from __future__ import annotations
>>>    2:
>>>    3: from importlib import resources
>>>    4:
>>>    5: import pytest_bazel
>>>    6: from mcp import types
>>>    7:
>>>    8: from mcp_infra.compositor.rendering import render_compositor_instructions
>>>    9: from mcp_infra.prefix import MCPMountPrefix
>>>   10: from mcp_infra.snapshots import RunningServerEntry
>>>   11:
>>>   12:
>>>   13: def test_template_packaged() -> None:
>>>   14:     # Ensure the template is available via importlib.resources
>>>   15:     pkg = "mcp_infra.compositor.templates"
>>>   16:     text = resources.files(pkg).joinpath("compositor_instructions.md.j2").read_text("utf-8")
>>>   17:     assert "Instructions" in text
>>>   18:
>>>   19:
>>>   20: def test_render_empty_states_returns_empty() -> None:
>>>   21:     out = render_compositor_instructions({})
>>>   22:     assert out == ""
>>>   23:
>>>   24:
>>>   25: def test_render_single_running_with_instructions() -> None:
>>>   26:     init = types.InitializeResult(
>>>   27:         protocolVersion="1.0",
>>>   28:         capabilities=types.ServerCapabilities(),
>>>   29:         serverInfo=types.Implementation(name="docker_exec", version="0.0.0"),
>>>   30:         instructions="Hello world",
>>>   31:     )
>>>   32:     state = RunningServerEntry(initialize=init, tools=[])
>>>   33:     out = render_compositor_instructions({MCPMountPrefix("docker_exec"): state})
>>>   34:     assert "The following MCP servers" in out
>>>   35:     assert "# docker_exec" in out
>>>   36:     assert "## Instructions" in out
>>>   37:     assert "Hello world" in out
>>>   38:
>>>   39:
>>>   40: if __name__ == "__main__":
>>>   41:     pytest_bazel.main()
```

### `misplaced-tests.yaml` / `occ-4`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/misplaced-tests.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/agent_server/test_runtime_timeout.py#L1-L50)

File: `agent_server/test_runtime_timeout.py` (L1-50)

> These test files live in agent_server/ but primarily test code in mcp_infra/
> (exec servers, compositor rendering, resources server). Tests for package X
> should not live in package Y just because Y depends on X. They belong under
> mcp_infra/ alongside the code they exercise.
>
> **Note:** Tests mcp_infra/exec/docker.py timeout handling

```
>>>    1: from __future__ import annotations
>>>    2:
>>>    3: import pytest
>>>    4: import pytest_bazel
>>>    5:
>>>    6: from mcp_infra.exec.docker.server import ContainerExecServer
>>>    7: from mcp_infra.exec.models import BaseExecResult, Exited, TimedOut, make_exec_input
>>>    8: from mcp_infra.naming import build_mcp_function
>>>    9: from mcp_infra.prefix import MCPMountPrefix
>>>   10: from mcp_infra.stubs.typed_stubs import ToolStub
>>>   11: from mcp_infra.testing.fixtures import make_container_opts
>>>   12:
>>>   13:
>>>   14: def _runtime_spec_persession(docker_client, image: str = "python:3.12-slim"):
>>>   15:     return ContainerExecServer(
>>>   16:         docker_client,
>>>   17:         make_container_opts(image),  # per-session container
>>>   18:     )
>>>   19:
>>>   20:
>>>   21: @pytest.mark.requires_docker
>>>   22: async def test_runtime_per_session_timeout_then_next_call_ok(
>>>   23:     compositor, compositor_client, async_docker_client
>>>   24: ) -> None:
>>>   25:     """Test runtime timeout and recovery without policy gateway."""
>>>   26:     # Mount runtime server and capture Mounted object
>>>   27:     mounted_runtime = await compositor.mount_inproc(
>>>   28:         MCPMountPrefix("runtime"), _runtime_spec_persession(async_docker_client)
>>>   29:     )
>>>   30:
>>>   31:     # Cause a host-side timeout: sleep longer than timeout_ms
>>>   32:     # Namespaced exec via Compositor
>>>   33:     stub = ToolStub(
>>>   34:         compositor_client,
>>>   35:         build_mcp_function(mounted_runtime.prefix, mounted_runtime.server.exec_tool.name),
>>>   36:         BaseExecResult,
>>>   37:     )
>>>   38:
>>>   39:     res_timeout = await stub(make_exec_input(["sh", "-lc", "sleep 3"], timeout_ms=500))
>>>   40:     assert isinstance(res_timeout.exit, TimedOut)
>>>   41:
>>>   42:     # Next call should work; container should have been restarted
>>>   43:     res_ok = await stub(make_exec_input(["/bin/echo", "-n", "ok"], timeout_ms=5000))
>>>   44:     assert isinstance(res_ok.exit, Exited)
>>>   45:     assert res_ok.exit.exit_code == 0
>>>   46:     assert (res_ok.stdout or "") == "ok"
>>>   47:
>>>   48:
>>>   49: if __name__ == "__main__":
>>>   50:     pytest_bazel.main()
```

### `ready-event-never-set.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/ready-event-never-set.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/agent_server/runtime/container.py#L233-L501)

File: `agent_server/runtime/container.py` (L233, L501)

> `AgentContainer._ready` is an `asyncio.Event` (line 233) awaited in
> `start()` (line 501) after `_post_msg(_StartMsg(...))` completes.
> `_ready.set()` is never called anywhere in the codebase. Every call to
> `start()` — and therefore `build_container()` — deadlocks on
> `self._ready.wait()`.

```
     230:         default_factory=asyncio.Queue, init=False
     231:     )
     232:     _actor_task: asyncio.Task | None = field(default=None, init=False)
>>>  233:     _ready: asyncio.Event = field(default_factory=asyncio.Event, init=False)
     234:     _closed: asyncio.Event = field(default_factory=asyncio.Event, init=False)
     235:     _stack: AsyncExitStack = field(default_factory=AsyncExitStack, init=False)
     236:     # Internal helpers/state
   ...
     498:     async def start(self, *, mcp_config: MCPConfig) -> None:
     499:         self._ensure_actor()
     500:         await self._post_msg(_StartMsg(mcp_config=mcp_config))
>>>  501:         await self._ready.wait()
     502:
     503:     async def close(self) -> CloseResult:
     504:         if self._actor_task is None:
```

### `trivial-forwarder.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/trivial-forwarder.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/props/cli/resources.py#L27-L34)

File: `props/cli/resources.py` (L27-34)

> `props.cli.resources.get_database_config` is a trivial forwarder that just
> calls `props.db.config.get_database_config`. The wrapper adds no caching,
> transformation, or typer-di–specific behavior — `Depends()` works identically
> with the original function. Callers should import from `props.db.config`
> directly and the module can be deleted.
>
> **Note:** Forwarder definition — body is just `return _get_database_config()`

```
      24: from props.db.config import DatabaseConfig, get_database_config as _get_database_config
      25:
      26:
>>>   27: def get_database_config() -> DatabaseConfig:
>>>   28:     """Get database configuration from environment variables.
>>>   29:
>>>   30:     Reads PostgreSQL connection parameters from environment (set by devenv or passed to containers).
>>>   31:
>>>   32:     typer-di calls this function only once per CLI invocation.
>>>   33:     """
>>>   34:     return _get_database_config()
      35:
      36:
      37: # NOTE: No get_docker_client() dependency function.
```

### `unguarded-singleton.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/unguarded-singleton.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/wt/client/wt_client.py#L321)

File: `wt/client/wt_client.py` (L321)

> Several call sites use `next(iter(collection))` to extract a value from a collection
> that is semantically expected to contain exactly one item. This silently discards any
> extra items instead of raising on unexpected cardinality. Use `more_itertools.one()` to
> enforce the single-item invariant (raises ValueError if zero or multiple items).
> The repo has a custom `unwrap_singleton` in `inventree_utils/beautifier/iter_util.py`
> that could be promoted to a repo-level helper as an alternative.
>
> **Note:** Requests status for a single worktree ID; response dict should have exactly one entry

```
     318:             return set(), set()
     319:
     320:         # Extract the single result
>>>  321:         item = next(iter(status_response.items.values()))
     322:         if isinstance(item.result, StatusResultError):
     323:             raise RpcError(ErrorCodes.INTERNAL_ERROR, item.result.error)
     324:
```

### `unguarded-singleton.yaml` / `occ-1`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/unguarded-singleton.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/wt/client/handlers.py#L104)

File: `wt/client/handlers.py` (L104)

> Several call sites use `next(iter(collection))` to extract a value from a collection
> that is semantically expected to contain exactly one item. This silently discards any
> extra items instead of raising on unexpected cardinality. Use `more_itertools.one()` to
> enforce the single-item invariant (raises ValueError if zero or multiple items).
> The repo has a custom `unwrap_singleton` in `inventree_utils/beautifier/iter_util.py`
> that could be promoted to a repo-level helper as an alternative.
>
> **Note:** Same pattern: requests status for a single worktree ID via get_status([info.wtid])

```
     101:     if not resp.items:
     102:         click.echo(f"❌ No status available for '{worktree_name}'")
     103:         return
>>>  104:     item = next(iter(resp.items.values()))
     105:     status = item.status
     106:     formatter.render_worktree_status_single(status.name, status, status.pr_info)
     107:
```

### `unguarded-singleton.yaml` / `occ-2`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/issues/unguarded-singleton.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2026-01-29-00/code/wt/server/handlers/test_status_handler.py#L58-L76)

File: `wt/server/handlers/test_status_handler.py` (L58, L76)

> Several call sites use `next(iter(collection))` to extract a value from a collection
> that is semantically expected to contain exactly one item. This silently discards any
> extra items instead of raising on unexpected cardinality. Use `more_itertools.one()` to
> enforce the single-item invariant (raises ValueError if zero or multiple items).
> The repo has a custom `unwrap_singleton` in `inventree_utils/beautifier/iter_util.py`
> that could be promoted to a repo-level helper as an alternative.
>
> **Note:** Test code: both tests assert len==1 then next(iter(...)); one() replaces both lines

```
      55:     response = await get_status(status_deps, StatusParams())
      56:
      57:     assert len(response.items) == 1
>>>   58:     item = next(iter(response.items.values()))
      59:
      60:     assert isinstance(item.result, StatusResultError)
      61:     assert "repository not found" in item.result.error
   ...
      73:     response = await get_status(status_deps, StatusParams())
      74:
      75:     assert len(response.items) == 1
>>>   76:     item = next(iter(response.items.values()))
      77:
      78:     assert isinstance(item.result, StatusResultOk)
      79:     assert item.result.status.branch_name == "feature-branch"
```

## ducktape/2025-09-03-00 (21)

### `cap-append-defaults.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/cap-append-defaults.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py#L133-L195)

File: `llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py` (L133-137, L154-156, L174-176, L194-195)

> Calls like `_cap_append(parts, chunk, MAX_PROMPT_CONTEXT_BYTES, "[Context truncated…]")` repeat the same
> constants at each site. Prefer giving `_cap_append` sensible defaults (or deriving the note from the cap)
> so callers only pass the varying pieces. This reduces duplication and drift risk across call sites.

### `diagnostics-broad-catch.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/diagnostics-broad-catch.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/mcp/sandboxed_jupyter_mcp/wrapper.py#L343)

File: `llm/adgn_llm/src/adgn_llm/mcp/sandboxed_jupyter_mcp/wrapper.py` (L343)

> During diagnostics the code catches broad Exception, prints a diagnostic message, and continues. In a
> diagnostics path
> this masks failures that were supposed to surface useful debug information — the wrapper should fail fast or
> at least
> propagate the error after logging full context.
>
> Diagnostics code should make problems visible and actionable. Silently continuing after printing a short
> message
> prevents test harnesses and callers from noticing failures and makes root-cause debugging much harder.
>
> Prefer: log full traceback and re-raise (or exit non-zero) so CI/tests detect the issue. Only suppress known,
> explicitly
> documented non-fatal exceptions.

### `docker-exec-unbounded-output.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/docker-exec-unbounded-output.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/mcp/docker_exec/server.py#L146-L250)

File: `llm/adgn_llm/src/adgn_llm/mcp/docker_exec/server.py` (L146-200, L241-250)

> Docker Exec MCP returns unbounded stdout/stderr data, which is hazardous for MCP/LLM agents and
> can also lead to process memory growth.
>
> Primary impact (MCP/LLM):
>
> - Tool responses are fed back into an LLM context. Returning megabytes of text will quickly
>   blow the caller’s context/window, causing truncation, failures, or severe quality drops.
>   MCP tools must bound returned payload size.
>
> Secondary impact (server memory):
>
> - The server accumulates stdout/stderr into bytearrays with no cap. Very chatty commands can
>   cause high memory usage or OOM over time.
>
> Observed (specimen paths):
>
> - llm/adgn_llm/src/adgn_llm/mcp/docker_exec/server.py collects into bytearrays without limits
>   and returns the full decoded strings in the tool payload.
>
> Acceptance criteria (bounded capture in MCP response):
>
> - Enforce an upper bound (bytes or characters) for stdout/stderr included in the tool return
>   (e.g., first N bytes, with a clear truncation note and total sizes).
> - Keep full data optional (e.g., tee to a temp file/log and return a path/reference), but the
>   MCP tool’s returned text must be bounded deterministically.
> - Document the cap and truncation behavior in the tool description so callers can plan.
>
> Optional (server memory hygiene):
>
> - Apply the same bound in the in-process accumulation path, or stream/tee to a file to avoid
>   unbounded memory growth while still allowing capped returns.

### `docker-mutable-singletons.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/docker-mutable-singletons.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/mcp/docker_exec/server.py#L53-L71)

File: `llm/adgn_llm/src/adgn_llm/mcp/docker_exec/server.py` (L53-58, L60-71)

> Module-level `_DOCKER_CLIENT` and `_CONTAINER_REF` introduce mutable global state that couples requests
> through hidden, process-wide singletons. This makes behavior order-dependent, complicates testing,
> and risks leaking configuration across calls.
>
> Prefer explicit dependency injection: pass a Docker client via parameters or a factory, or manage per-request
> context that resolves the container ref at call time. Keep state local to the request boundary.

### `enforce-single-total-cap.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/enforce-single-total-cap.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py#L133-L205)

File: `llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py` (L133-137, L154-156, L174-176, L194-195, L201-205)

> Current caps are applied per git output block (status / name-status / log / diff), so the assembled
> prompt can reach many× the nominal cap. Prefer a single accumulator-based total cap enforced over the
> fully assembled prompt, or track remaining bytes across calls to `_cap_append` to share the budget.
>
> This yields predictable size, avoids double work, and makes tradeoffs explicit between sections.

### `exceptions-for-control-flow.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/exceptions-for-control-flow.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py#L304)

File: `llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py` (L304)

> Do not use try/except to detect normal, non-error conditions. Reserve exceptions for unexpected situations.
> The current "first commit" detection relies on catching a diff failure, which can also swallow unrelated
> errors.
> Prefer a positive repository capability/condition check with early bailout. Example pattern:
>
> - If we're in the 90% normal case (without executing a failing operation), run the normal path.
> - Else, handle the 10% case explicitly.
>   As a reviewer, seeing try/except signals "what's on fire" (unexpected), not a routine precondition check.
>
> **Note:** try/except used to detect first commit instead of positive check

### `gitpython-over-shell.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/gitpython-over-shell.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py#L731-L739)

File: `llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py` (L731-739)

> `_get_editor` shells out via `asyncio.create_subprocess_exec("git", "var", "GIT_EDITOR", ...)` to
> obtain the editor. Prefer using the repo API directly (e.g., `repo.git.var("GIT_EDITOR")`) or a
> config reader fallback (`repo.config_reader().get_value("core", "editor", default)`). This reduces
> subprocess boilerplate and simplifies control flow.

### `legacy-policyconfig-shim.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/legacy-policyconfig-shim.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/mcp/sandboxed_jupyter_mcp/wrapper.py#L23-L27)

File: `llm/adgn_llm/src/adgn_llm/mcp/sandboxed_jupyter_mcp/wrapper.py` (L23-27)

> The wrapper contains a legacy PolicyConfig shim that exists only for import compatibility with older tests.
> Keeping dead shims because "tests still reference it" is not a sufficient reason to retain the code: tests
> should be
> updated to the canonical model or provided a test-only shim.
>
> Why this is bad:
>
> - It preserves dead/unused code paths that increase maintenance burden and cognitive load.
> - New readers assume the shim is live behavior and may write code to support it, increasing cruft.
> - Tests depending on obsolete shims should be migrated or wrapped in explicit test fixtures rather than
>   perpetuating
>   legacy surface area.

### `max-prompt-cap-name.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/max-prompt-cap-name.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py#L60-L205)

File: `llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py` (L60, L135-137, L155-156, L175-176, L194-195, L201-205)

> The constant name `MAX_PROMPT_CONTEXT_BYTES` uses two near-synonyms in this code path ("prompt" and
> "context").
> Either pick one term and scope it correctly, or enforce a true global prompt cap:
>
> Options:
>
> - Rename to reflect true scope (per-block cap): e.g.,
>   `MAX_PROMPT_GIT_OUTPUT_BYTES` (applies to each appended block)
> - Or adopt a global `MAX_PROMPT_BYTES` and enforce an overall cap,
>   leaving block-level caps as internal helpers
>
> This reduces ambiguity, communicates scope precisely, and prevents misinterpretation.

### `redundant-parallel-names.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/redundant-parallel-names.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py#L906-L922)

File: `llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py` (L906-922)

> The editor flow uses redundant parallel variable names (`final_text` and `content_before`) that mirror each
> other
> without adding clarity. Keep a single source variable to reduce cognitive load and avoid confusion about which
> represents the canonical value.

### `remove-openai-key-plumb.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/remove-openai-key-plumb.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/mini_codex/agent.py#L45-L111)

File: `llm/adgn_llm/src/adgn_llm/mini_codex/agent.py` (L45-55, L100-111)

> The OpenAI SDK already reads `OPENAI_API_KEY` and base URL env vars; hand-rolling a client factory that
> fetches
> env vars duplicates configuration paths and adds code surface without value.
>
> Prefer:
>
> - Call `openai.OpenAI()` directly and let the SDK read environment variables; or
> - Inject a client (DI) from the caller/tests to keep construction policy out of core logic.
>
> This reduces duplication and makes tests simpler (just pass a client/fake).

### `responses-turn-duplicate.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/responses-turn-duplicate.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/mini_codex/cli.py#L188-L306)

File: `llm/adgn_llm/src/adgn_llm/mini_codex/cli.py` (L188-218, L276-306)

> `responses_turn` and `responses_followup_with_tool_outputs` in the CLI duplicate ~20 lines of logic:
> assembling instructions (with optional MCP block), listing tools, building the payload, and
> calling Responses. This copy/paste raises drift risk and splits responsibility between CLI and agent.
>
> Preferred design:
>
> - Keep agent.py as the single owner of the agent loop and Responses flow (instructions assembly,
>   tools listing, payload construction, result parsing).
> - Make cli.py a thin wrapper that delegates to the agent (or a single helper) rather than repeating logic.
>
> Concretely: extract a shared helper (or call through to agent) used by both paths, removing the duplicate
> try/except + instruction assembly + tools list + responses.create blocks.

### `runner-branch-duplication.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/runner-branch-duplication.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py#L590-L621)

File: `llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py` (L590-621)

> ParallelTaskRunner.create_and_run duplicates runner construction and update loop across branches; only output
> streaming
> differs.
> Prefer a single shared trunk: compute precommit_task (real or noop) and master_fd, construct the runner once,
> start
> the
> update loop once, and stream output only if master_fd is not None. This keeps the main path flat (early
> bailout for
> no-precommit).

### `scoped-try-except-swallow.yaml` / `occ-4`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/scoped-try-except-swallow.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/mini_codex/mcp_manager.py#L55-L59)

File: `llm/adgn_llm/src/adgn_llm/mini_codex/mcp_manager.py` (L55-59)

> Scoped try/except blocks swallow errors instead of failing loudly.
> Where there is no specific recovery/handling need, do not catch at all — let exceptions bubble normally.
> Where there is a specific reason to handle, catch only the narrow exception and do not swallow silently (log
> and/or
> re-raise as appropriate).
>
> **Note:** mkdir failure silently falls back to cwd, hiding operational problems

### `timeout-ms-propagation.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/timeout-ms-propagation.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/mini_codex/local_tools.py#L28-L127)

File: `llm/adgn_llm/src/adgn_llm/mini_codex/local_tools.py` (L28-43, L50-90, L110-127)

> exec_handler converts timeout_ms to seconds early with int(timeout_ms / 1000), truncating
> sub-second precision (1500ms becomes 1s, 500ms becomes 0→1s). Timeout should be propagated
> as milliseconds (int) throughout the call chain and only divided by 1000.0 at the final
> subprocess.communicate() call. This requires changing: exec_handler to pass timeout_ms
> directly, \_run_in_sandbox(timeout_s: int) → \_run_in_sandbox(timeout_ms: int),
> \_run_proc(timeout_s: int) → \_run_proc(timeout_ms: int), and \_run_proc to convert at
> communicate: p.communicate(timeout=timeout_ms / 1000.0). Python >=3.11 is required
> and subprocess.communicate() has supported float timeout since Python 3.3.

### `timeout-noop-branch.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/timeout-noop-branch.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/mcp/docker_exec/server.py#L181-L183)

File: `llm/adgn_llm/src/adgn_llm/mcp/docker_exec/server.py` (L181-183)

> The timeout branch is a literal no-op:
>
> if timed_out: # We cannot reliably kill the exec unless wrapper handled it; return best-effort
> pass
>
> Timeout handling in this module:
>
> - With USE_CONTAINER_TIMEOUT_WRAPPER=1, commands are wrapped in `timeout -s TERM <secs>` inside the container
>   (see
>   lines 27–31), so the process is actually signaled on expiry.
> - Without the wrapper, we stop reading and return ExecResult with `timed_out=True`,
>   but the container process may keep running. Tests only assert `timed_out`; they do not verify termination.
>
> This is a footgun: timeouts can exceed and leave processes running. At the very least, document this behavior
> prominently and surface explicit return markers (e.g., `timeout_enforced=false` or `kill_attempted=false`) so
> callers
> can react.
>
> Preferred fix: require an always-correct timeout path. If a timeout is requested and the wrapper is
> unavailable, fail
> fast (refuse to run) instead of best-effort; or ensure the implementation enforces termination reliably.
> Delete the
> empty branch.

### `timeout-units-ambiguous.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/timeout-units-ambiguous.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/mcp/docker_exec/server.py#L55)

File: `llm/adgn_llm/src/adgn_llm/mcp/docker_exec/server.py` (L55)

> Constants representing timeouts should carry units in their type or name. `_DEFAULT_TIMEOUT: float | None =
None` is
> ambiguous about units.
>
> Prefer one of two patterns:
>
> - Use a timedelta, e.g. `DEFAULT_TIMEOUT = timedelta(seconds=30)`, and name it DEFAULT_TIMEOUT.
> - If storing a numeric value, include the unit in the name and type,
>   e.g. `DEFAULT_TIMEOUT_S: int | None = None`.
>
> Benefits: reduces confusion about whether a timeout is seconds, milliseconds, or fractional seconds; makes
> call sites
> clearer and avoids silent misconfigurations.

### `trivial-wrapper-main.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/trivial-wrapper-main.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/mcp/sandboxed_jupyter_mcp/cli.py#L6-L7)

File: `llm/adgn_llm/src/adgn_llm/mcp/sandboxed_jupyter_mcp/cli.py` (L6-7)

> The CLI `main()` in `mcp/sandboxed_jupyter_mcp/cli.py` merely delegates to `wrapper.main()` without adding any
> value
> (no
> argument transformation, validation, or help text).
> One-line passthrough wrappers like this add indirection and lines of code for no benefit. Prefer calling the
> implementation directly from entry points or consolidating the tiny delegating main into the wrapper to reduce
> churn
> and
> improve readability.

### `truncation-msg-hardcoded.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/truncation-msg-hardcoded.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py#L135-L205)

File: `llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py` (L135-136, L154-156, L174-176, L193-195, L201-205)

> The truncation note is hardcoded as "[Context truncated to 100 KiB]" in multiple places, while the cap
> is driven by MAX_PROMPT_CONTEXT_BYTES. This duplicates the limit in string form and risks drift.
>
> Prefer a single source of truth: derive the human text from the cap (e.g., f"[Context truncated to
>
> > {MAX_PROMPT_CONTEXT_BYTES // 1024} KiB]")
> > or use a generic stable marker like "[Context truncated]". Keep the message in one place and reuse it.

### `tty-guard-early-bailout.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/tty-guard-early-bailout.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py#L715-L721)

File: `llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py` (L715-721)

> The TTY guard should use an early bailout to avoid unnecessary nesting.
> Instead of nesting the main logic under `if sys.stdout.isatty(): ...`, invert the condition and return/skip
> when not a
> TTY, then run the terminal sizing at the base level.

### `unused-prev-msg-default.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-09-03-00/issues/unused-prev-msg-default.yaml) · [code](https://github.com/agentydragon/ducktape/blob/4ad33013af27e159863bed92ffcfdb55b388e46c/llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py#L280-L281)

File: `llm/adgn_llm/src/adgn_llm/git_commit_ai/cli.py` (L280-281)

> The parameter is declared with a default that callers never use:
>
> previous_message: str | None = None
>
> Unused defaults add unnecessary degrees of freedom and complicate API contracts.
> Prefer tightening the signature: drop the default (require an explicit value from callers)
> or make the parameter mandatory only where needed via a higher-level object.

## ducktape/2025-11-20-00 (21)

### `collection-params-empty-tuple.yaml` / `occ-2` [P20]

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/collection-params-empty-tuple.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/agent/persist/sqlite.py#L99-L102)

File: `adgn/src/adgn/agent/persist/sqlite.py` (L99, L101-102)

> Functions accept collection parameters as Optional, defaulting to None, then
> check for None and convert to empty collection. Should use empty collection
> as default instead.
>
> Benefits:
>
> - Simpler type: no Optional/union with None
> - No None checks or reassignments needed
> - Empty tuple is immutable and safe as default
> - Clearer intent: "no items" vs "missing value"
> - Empty collections are falsy if bool check needed
>
> This is a standard Python idiom for collection parameters.
>
> **Note:** attach/detach default to None, then reassigned with `attach or {}` and `detach if detach is not None else []`

```
      96:             await session.commit()
      97:
      98:     async def patch_agent_specs(
>>>   99:         self, agent_id: AgentID, *, attach: dict[str, MCPConfig] | None = None, detach: list[str] | None = None
     100:     ) -> MCPConfig:
>>>  101:         attach = attach or {}
>>>  102:         detach = detach if detach is not None else []
     103:         async with self._session() as session:
     104:             result = await session.execute(select(Agent).where(Agent.id == agent_id))
     105:             agent = result.scalar_one_or_none()
```

### `collection-params-empty-tuple.yaml` / `occ-3` [P20]

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/collection-params-empty-tuple.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/agent/persist/__init__.py#L141)

File: `adgn/src/adgn/agent/persist/__init__.py` (L141)

> Functions accept collection parameters as Optional, defaulting to None, then
> check for None and convert to empty collection. Should use empty collection
> as default instead.
>
> Benefits:
>
> - Simpler type: no Optional/union with None
> - No None checks or reassignments needed
> - Empty tuple is immutable and safe as default
> - Clearer intent: "no items" vs "missing value"
> - Empty collections are falsy if bool check needed
>
> This is a standard Python idiom for collection parameters.
>
> **Note:** Protocol signature uses Optional instead of default empty collection

```
     138:     async def create_agent(self, *, mcp_config: MCPConfig, metadata: AgentMetadata) -> AgentID: ...
     139:     async def update_agent_specs(self, agent_id: AgentID, *, mcp_config: MCPConfig) -> None: ...
     140:     async def patch_agent_specs(
>>>  141:         self, agent_id: AgentID, *, attach: dict[str, MCPConfig] | None = None, detach: list[str] | None = None
     142:     ) -> MCPConfig: ...
     143:     async def list_agents(self) -> list[AgentRow]: ...
     144:     async def get_agent(self, agent_id: AgentID) -> AgentRow | None: ...
```

### `approvals-pending-wrong-attributes.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/approvals-pending-wrong-attributes.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/agent/mcp_bridge/servers/agents.py#L400-L422)

File: `adgn/src/adgn/agent/mcp_bridge/servers/agents.py` (L400-422)

> approvals_pending_global builds URIs and JSON by accessing approval.call_id, approval.tool,
> and approval.args, but PendingApproval only exposes tool_call (a ToolCall object) and timestamp.
> The code raises AttributeError on every invocation because these attributes don't exist at the
> PendingApproval level - they need to be accessed via approval.tool_call.call_id,
> approval.tool_call.name, and approval.tool_call.args_json respectively.

```
     397:         mime_type="application/json",
     398:         description="Global mailbox: all pending approvals across all agents (returns multiple content blocks)",
     399:     )
>>>  400:     async def approvals_pending_global():
>>>  401:         """Each approval is a separate MCP TextResourceContents block.
>>>  402:
>>>  403:         Crashes if any agent fails (no exception swallowing).
>>>  404:         """
>>>  405:         content_blocks: list[mcp_types.TextResourceContents] = []
>>>  406:
>>>  407:         for agent_id in registry.known_agents():
>>>  408:             infra = await registry.get_infrastructure(agent_id)
>>>  409:             pending_approvals = _convert_pending_approvals(infra.approval_hub.pending)
>>>  410:
>>>  411:             for approval in pending_approvals:
>>>  412:                 approval_uri = f"resource://agents/{agent_id}/approvals/{approval.call_id}"
>>>  413:                 approval_data = {
>>>  414:                     "agent_id": agent_id,
>>>  415:                     "call_id": approval.call_id,
>>>  416:                     "tool": approval.tool,
>>>  417:                     "args": approval.args,
>>>  418:                     "timestamp": approval.timestamp.isoformat(),
>>>  419:                 }
>>>  420:                 block = mcp_types.TextResourceContents(
>>>  421:                     uri=approval_uri, mimeType="application/json", text=json.dumps(approval_data)
>>>  422:                 )
     423:                 content_blocks.append(block)
     424:
     425:         return mcp_types.ReadResourceResult(contents=content_blocks)
```

### `char-len-vs-byte-len.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/char-len-vs-byte-len.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/agent/agent.py#L97-L177)

File: `adgn/src/adgn/agent/agent.py` (L97, L101, L177)

> `_dump_call_tool_result` (line 101) checks `len(result) > MAX_TOOL_RESULT_BYTES`
> where `result` is a Python `str` and `MAX_TOOL_RESULT_BYTES = 10 * 1024 * 1024`
> (line 177). `len(str)` returns character count, not byte count. Because
> `json.dumps(..., ensure_ascii=False)` (line 97) preserves multi-byte characters,
> a string with non-ASCII content (e.g., CJK text, emoji) can have significantly
> more UTF-8 bytes than characters, bypassing the guard. The error message
> (line 103) also reports `len(result)` as MB, further misrepresenting the size.

```
      94:     Dumps a compact JSON with native snake_case keys to avoid lossy remapping.
      95:     """
      96:
>>>   97:     result = json.dumps(serialize_tool_result_compact(res), ensure_ascii=False)
      98:
      99:     # Safety check: OpenAI has a 10MB limit for input strings
     100:     # Fail fast if tool output is too large to prevent API errors
>>>  101:     if len(result) > MAX_TOOL_RESULT_BYTES:
     102:         error_msg = (
     103:             f"Tool output too large: {len(result) / (1024 * 1024):.1f}MB "
     104:             f"exceeds max {MAX_TOOL_RESULT_BYTES / (1024 * 1024):.0f}MB. "
   ...
     174: SYSTEM_INSTRUCTIONS = "You are a code agent. Be concise."
     175:
     176: # Size limits (bytes)
>>>  177: MAX_TOOL_RESULT_BYTES = 10 * 1024 * 1024  # 10 MiB
     178:
     179:
     180: def _tool_choice_from_policy(policy: ToolPolicy) -> ToolChoice:
```

### `parse-response-should-not-exist.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/parse-response-should-not-exist.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/llm/sysrw/openai_typing.py#L111-L123)

File: `adgn/src/adgn/llm/sysrw/openai_typing.py` (L111-123)

> `parse_response_messages` and `parse_chat_messages` accept `Any` and validate via
> TypeAdapter at runtime. Callers pass untyped dicts — e.g., `translation.py:149` builds
> dicts via `.model_dump()` then immediately re-validates them, and `run_eval.py` has
> multiple functions typed `(inp: Any)` that just forward to `parse_response_messages`.
>
> The data should be typed at source: callers should build/hold `list[ResponseOutputMessage]`
> directly instead of round-tripping through dicts. This eliminates the need for runtime
> validation functions and stops `Any` from spreading through the codebase.
>
> **Note:** parse_response_messages function

```
     108: # Removed parse_tool_call and extract_*_tool_call_info - no longer needed since we work with typed objects directly
     109:
     110:
>>>  111: def parse_response_messages(messages: Any) -> list[ResponseOutputMessage] | None:
>>>  112:     """Parse messages into validated ResponseOutputMessage objects.
>>>  113:
>>>  114:     Args:
>>>  115:         messages: Unvalidated external payload (typically from OpenAI API response).
>>>  116:                   Structured validation happens via TypeAdapter within function.
>>>  117:
>>>  118:     Returns:
>>>  119:         Validated list of ResponseOutputMessage objects, or None if messages is falsy.
>>>  120:     """
>>>  121:     if not messages:
>>>  122:         return None
>>>  123:     return TypeAdapter(list[ResponseOutputMessage]).validate_python(messages)
     124:
     125:
     126: def dump_response_messages(messages: list[ResponseOutputMessage]) -> list[dict[str, Any]]:
```

### `parse-response-should-not-exist.yaml` / `occ-1`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/parse-response-should-not-exist.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/llm/sysrw/openai_typing.py#L136-L148)

File: `adgn/src/adgn/llm/sysrw/openai_typing.py` (L136-148)

> `parse_response_messages` and `parse_chat_messages` accept `Any` and validate via
> TypeAdapter at runtime. Callers pass untyped dicts — e.g., `translation.py:149` builds
> dicts via `.model_dump()` then immediately re-validates them, and `run_eval.py` has
> multiple functions typed `(inp: Any)` that just forward to `parse_response_messages`.
>
> The data should be typed at source: callers should build/hold `list[ResponseOutputMessage]`
> directly instead of round-tripping through dicts. This eliminates the need for runtime
> validation functions and stops `Any` from spreading through the codebase.
>
> **Note:** parse_chat_messages function

```
     133:     return [TypeAdapter(dict[str, Any]).validate_python(msg) for msg in messages]
     134:
     135:
>>>  136: def parse_chat_messages(messages: Any) -> list[ChatCompletionMessageParam] | None:
>>>  137:     """Parse messages into validated ChatCompletionMessageParam objects.
>>>  138:
>>>  139:     Args:
>>>  140:         messages: Unvalidated external payload (typically from stored state or API).
>>>  141:                   Structured validation happens via TypeAdapter within function.
>>>  142:
>>>  143:     Returns:
>>>  144:         Validated list of ChatCompletionMessageParam objects, or None if messages is falsy.
>>>  145:     """
>>>  146:     if not messages:
>>>  147:         return None
>>>  148:     return TypeAdapter(list[ChatCompletionMessageParam]).validate_python(messages)
     149:
     150:
     151: # Remove this function - parse the data into the right type first instead of handling unions
```

### `pydantic-write-path.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/pydantic-write-path.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/agent/persist/handler.py#L102-L146)

File: `adgn/src/adgn/agent/persist/handler.py` (L102-103, L110, L145-146)

> Pre-serialization of Pydantic models before passing to persistence layer.
>
> Calls model_dump() at caller site (lines 102-103, 110, 145-146) before passing
> to persistence methods. This violates separation of concerns - caller shouldn't
> know about persistence format.
>
> Anti-pattern: Serialization at caller site instead of callee. Correct approach:
> append_event should accept typed EventRecord payload, ResponsePayload should
> accept Response model, and serialization should happen inside persistence layer.
>
> Benefits:
>
> - Type safety preserved across call boundary
> - Single serialization point (DRY)
> - Clearer responsibility boundaries
> - Caller doesn't need to know persistence format
> - Easier to change serialization strategy later

```
      99:             self._last_run_id = rid
     100:             self._seq = 0
     101:         self._seq += 1
>>>  102:         # Convert TypedPayload to dict for persistence
>>>  103:         payload_dict = payload.model_dump(mode="json", exclude_none=True)
     104:         self._spawn(
     105:             self._persistence.append_event(
     106:                 run_id=rid,
     107:                 seq=self._seq,
     108:                 ts=self._now(),
     109:                 type=type,
>>>  110:                 payload=payload_dict,
     111:                 call_id=call_id,
     112:                 tool_key=tool_key,
     113:             )
   ...
     142:
     143:     def on_response(self, evt: Response) -> None:
     144:         # Convert Response to ResponsePayload; for now pass full dumped content
>>>  145:         content_dict = evt.model_dump(mode="json", exclude_none=True)
>>>  146:         self._record_event(type=EventType.RESPONSE, payload=ResponsePayload(content=content_dict))
```

### `registry-get-missing.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/registry-get-missing.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/agent/server/status_shared.py#L114)

File: `adgn/src/adgn/agent/server/status_shared.py` (L114)

> InfrastructureRegistry.get() method is called but does not exist in the class definition.
>
> The calls should likely use get_running_infrastructure() instead,
> based on the usage pattern where the result is checked for None.
>
> **Note:** Called in build_agent_status_core: c = registry.get(agent_id)

```
     111:     registry = app.state.registry
     112:     persistence = app.state.persistence
     113:
>>>  114:     c = registry.get(agent_id)
     115:     present = c is not None
     116:
     117:     # UI + approvals + active run
```

### `registry-get-missing.yaml` / `occ-1`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/registry-get-missing.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/agent/mcp_bridge/servers/agents.py#L516)

File: `adgn/src/adgn/agent/mcp_bridge/servers/agents.py` (L516)

> InfrastructureRegistry.get() method is called but does not exist in the class definition.
>
> The calls should likely use get_running_infrastructure() instead,
> based on the usage pattern where the result is checked for None.
>
> **Note:** Called in agent_ui_state_resource: runtime = registry.get(agent_id)

```
     513:     )
     514:     async def agent_ui_state_resource(agent_id: AgentID) -> str:
     515:         """UI state (optional, only if UI server attached)."""
>>>  516:         runtime = registry.get(agent_id)
     517:         if not runtime or not runtime.runtime.session:
     518:             raise ValueError(f"Agent {agent_id} has no session")
     519:
```

### `return-result-not-reconstruct.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/return-result-not-reconstruct.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/agent/runtime/registry.py#L43-L44)

File: `adgn/src/adgn/agent/runtime/registry.py` (L43-44)

> AgentContainer.close() deconstructs CloseResult to rebuild identical dict
> (registry.py:43-44):
>
> result = await self.running.close() # Returns CloseResult
> return {"drained": result.drained, "error": result.error}
>
> CloseResult is a dataclass with drained and error fields (running.py:28-31).
> The code extracts these fields to create a dict with the same structure.
>
> Should return the result directly:
> return await self.running.close()
>
> Or inline the call:
> await self.runtime.close()
> return await self.running.close()
>
> Benefits:
>
> - No useless reconstruction
> - Preserves type information (CloseResult vs untyped dict)
> - Clearer intent: propagate result from running.close()
> - Less code
>
> Investigation shows return value unused at call site (registry.py:105),
> so dict reconstruction serves no purpose. If serialization needed, use
> dataclasses.asdict() or Pydantic.

```
      40:     async def close(self):
      41:         """Lifecycle management - close all components together."""
      42:         await self.runtime.close()
>>>   43:         result = await self.running.close()
>>>   44:         return {"drained": result.drained, "error": result.error}
      45:
      46:
      47: @dataclass
```

### `snapshot-misses-active-run-at-start.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/snapshot-misses-active-run-at-start.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/agent/server/runtime.py#L399-L402)

File: `adgn/src/adgn/agent/server/runtime.py` (L399-402)

> AgentSession.\_run_impl builds and sends a snapshot before setting self.active_run. Line 399
> calls `await self._manager.send_payload(await self.build_snapshot())` but self.active_run isn't
> assigned until line 400-402. Since build_snapshot only includes run metadata when
> self.active_run is non-None, the startup snapshot always contains active_run_id=None, empty
> pending_approvals, and no SnapshotDetails. UI clients reading the snapshot resource never learn
> that a run started until the next snapshot emission (typically at run completion). The
> active_run assignment should be moved before the build_snapshot call so the snapshot accurately
> reflects that a run is active.

```
     396:             # state (not incremental run_status) update immediately.
     397:             # This helps early UI elements like the Abort button appear
     398:             # deterministically even if they don't consume run_status events.
>>>  399:             await self._manager.send_payload(await self.build_snapshot())
>>>  400:             self.active_run = RunState(
>>>  401:                 run_id=run_id, status=UiRunStatus.RUNNING, started_at=started, pending_approvals=[], last_event_id=None
>>>  402:             )
     403:             self._run_counter += 1
     404:             # Notify MCP bridge of session state change (run started)
     405:             if self._manager._session_state_notifier is not None:
```

### `str-fallback-on-structured.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/str-fallback-on-structured.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/llm/sysrw/openai_typing.py#L85-L104)

File: `adgn/src/adgn/llm/sysrw/openai_typing.py` (L85, L92, L99, L104)

> chat_param_message_content_as_text (lines 75-105) claims to "extract text content", but when
> content is not a plain string (e.g., multi-part ChatCompletion\*MessageParam with structured
> content like [{'type': 'text', 'text': 'hi'}]), it falls back to str(content) (lines 85, 92,
> 99, 104), returning the Python repr of the structure instead of the actual text. This is
> misleading and makes it easy to abuse the API - callers receive strings like "[{'type':
>
> > 'text', 'text': 'hi'}]" and may not realize they're getting repr output rather than extracted
> > text. The function should be designed to make abuse hard: it should expect text-only content
> > and either raise an exception or return None (with a return type like str | None) when called
> > on non-text content, forcing callers to handle structured content explicitly. The name and
> > docstring should also clarify that this is only for text-only messages.

```
      82:             content = message.get("content")
      83:             if isinstance(content, str):
      84:                 return content
>>>   85:             return str(content) if content else ""
      86:         case MessageRole.USER:
      87:             # ChatCompletionUserMessageParam - content is required
      88:             content = message["content"]
      89:             if isinstance(content, str):
      90:                 return content
      91:             return str(content)
>>>   92:         case MessageRole.SYSTEM:
      93:             # ChatCompletionSystemMessageParam - content is required
      94:             content = message["content"]
      95:             if isinstance(content, str):
      96:                 return content
      97:             return str(content)
      98:         case MessageRole.TOOL | MessageRole.FUNCTION | MessageRole.DEVELOPER:
>>>   99:             # Other message types - handle gracefully
     100:             content = message.get("content")
     101:             if isinstance(content, str):
     102:                 return content
     103:             return str(content) if content else ""
>>>  104:         case _:
     105:             raise ValueError(f"Unhandled MessageRole: {role}")
     106:
     107:
```

### `stub-convenience-stack-method.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/stub-convenience-stack-method.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/agent/runtime/infrastructure.py#L180-L182)

File: `adgn/src/adgn/agent/runtime/infrastructure.py` (L180-182)

> Creating typed server stubs requires verbose boilerplate (infrastructure.py:180-186):
>
> reader_client = Client(reader_server)
> await stack.enter_async_context(reader_client)
> policy_reader = PolicyReaderStub(TypedClient(reader_client))
>
> approver_client = Client(approver_server)
> await stack.enter_async_context(approver_client)
> policy_approver = PolicyApproverStub(TypedClient(approver_client))
>
> This 3-line pattern repeats for every stub. Should provide convenience method:
>
> policy_reader = await PolicyReaderStub.for_server(stack, reader_server)
> policy_approver = await PolicyApproverStub.for_server(stack, approver_server)
>
> Or even simpler with context manager protocol on stub class.
>
> The for_server method would encapsulate:
>
> 1. Create Client from server
> 2. Enter into async context stack
> 3. Wrap in TypedClient
> 4. Return stub instance
>
> Benefits:
>
> - DRY: pattern in one place
> - Less error-prone: can't forget context manager entry
> - Clearer intent: "create stub from server"
> - Reduces line count 3:1
>
> This suggests base class method or helper function in server stub framework.
>
> **Note:** PolicyReaderStub creation boilerplate

```
     177:
     178:         approver_server = ApprovalPolicyAdminServer(engine=approval_engine, name=APPROVAL_POLICY_SERVER_NAME_APPROVER)
     179:
>>>  180:         reader_client = Client(reader_server)
>>>  181:         await stack.enter_async_context(reader_client)
>>>  182:         policy_reader = PolicyReaderStub(TypedClient(reader_client))
     183:
     184:         approver_client = Client(approver_server)
     185:         await stack.enter_async_context(approver_client)
```

### `stub-convenience-stack-method.yaml` / `occ-1`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/stub-convenience-stack-method.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/agent/runtime/infrastructure.py#L184-L186)

File: `adgn/src/adgn/agent/runtime/infrastructure.py` (L184-186)

> Creating typed server stubs requires verbose boilerplate (infrastructure.py:180-186):
>
> reader_client = Client(reader_server)
> await stack.enter_async_context(reader_client)
> policy_reader = PolicyReaderStub(TypedClient(reader_client))
>
> approver_client = Client(approver_server)
> await stack.enter_async_context(approver_client)
> policy_approver = PolicyApproverStub(TypedClient(approver_client))
>
> This 3-line pattern repeats for every stub. Should provide convenience method:
>
> policy_reader = await PolicyReaderStub.for_server(stack, reader_server)
> policy_approver = await PolicyApproverStub.for_server(stack, approver_server)
>
> Or even simpler with context manager protocol on stub class.
>
> The for_server method would encapsulate:
>
> 1. Create Client from server
> 2. Enter into async context stack
> 3. Wrap in TypedClient
> 4. Return stub instance
>
> Benefits:
>
> - DRY: pattern in one place
> - Less error-prone: can't forget context manager entry
> - Clearer intent: "create stub from server"
> - Reduces line count 3:1
>
> This suggests base class method or helper function in server stub framework.
>
> **Note:** PolicyApproverStub creation boilerplate

```
     181:         await stack.enter_async_context(reader_client)
     182:         policy_reader = PolicyReaderStub(TypedClient(reader_client))
     183:
>>>  184:         approver_client = Client(approver_server)
>>>  185:         await stack.enter_async_context(approver_client)
>>>  186:         policy_approver = PolicyApproverStub(TypedClient(approver_client))
     187:
     188:         return (policy_reader, policy_approver)
     189:
```

### `token-role-invalid-state.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/token-role-invalid-state.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/agent/server/mcp_routing.py#L76-L97)

File: `adgn/src/adgn/agent/server/mcp_routing.py` (L76-97, L86)

> TokenRole + agent_id (mcp_routing.py:76-97) accepts role and agent_id
> separately, allowing invalid state (AGENT role without agent_id). Should
> use discriminated union (HumanTokenInfo | AgentTokenInfo) to make invalid
> state unrepresentable.
>
> Current code has runtime check `if not agent_id` at line 86 to handle
> the invalid state that the type system allows.
>
> Using discriminated union provides:
>
> - Type safety: invalid states unrepresentable
> - No runtime validation needed
> - Clear type contracts in signatures

```
      73:                     return auth_value[7:]  # Strip "Bearer " prefix
      74:         return None
      75:
>>>   76:     async def _get_backend_app(self, role: TokenRole, agent_id: str | None) -> ASGIApp:
>>>   77:         """Get or create backend ASGI app for the given role/agent_id."""
>>>   78:         if role == TokenRole.HUMAN:
>>>   79:             backend_key = "human"
>>>   80:             if backend_key not in self._backend_apps:
>>>   81:                 # Use the agents management server's HTTP app
>>>   82:                 self._backend_apps[backend_key] = self.agents_server.http_app()  # type: ignore[assignment]
>>>   83:             return self._backend_apps[backend_key]
>>>   84:
>>>   85:         if role == TokenRole.AGENT:
>>>   86:             if not agent_id:
>>>   87:                 raise ValueError("Agent role requires agent_id")
>>>   88:
>>>   89:             backend_key = f"agent:{agent_id}"
>>>   90:             if backend_key not in self._backend_apps:
>>>   91:                 # Get the agent's compositor HTTP app
>>>   92:                 container = await self.registry.ensure_live(AgentID(agent_id), with_ui=False)
>>>   93:                 compositor_app = container.running.compositor.http_app()
>>>   94:                 self._backend_apps[backend_key] = compositor_app  # type: ignore[assignment]
>>>   95:             return self._backend_apps[backend_key]
>>>   96:
>>>   97:         raise ValueError(f"Unknown role: {role}")
      98:
      99:     async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
     100:         """Route request to appropriate backend based on token."""
```

### `token-table-pydantic-model.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/token-table-pydantic-model.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/agent/server/mcp_routing.py#L37-L116)

File: `adgn/src/adgn/agent/server/mcp_routing.py` (L37-40, L115, L116)

> TOKEN_TABLE uses nested untyped dicts (mcp_routing.py:37-40):
>
> TOKEN_TABLE: dict[str, dict[str, str]] = {
> "human-token-123": {"role": "human"},
> "agent-token-abc": {"role": "agent", "agent_id": "agent-1"},
> }
>
> Problems:
>
> - No type safety: can't validate field presence
> - No autocomplete for fields (role, agent_id)
> - Field names are magic strings
> - Can't distinguish required vs optional fields
> - Code accesses with dict["role"], dict.get("agent_id")
>
> Should define Pydantic model:
> class TokenInfo(BaseModel):
> role: TokenRole # Already a StrEnum
> agent_id: AgentID | None = None
>
> TOKEN_TABLE: dict[str, TokenInfo] = {
> "human-token-123": TokenInfo(role=TokenRole.HUMAN),
> "agent-token-abc": TokenInfo(role=TokenRole.AGENT, agent_id="agent-1"),
> }
>
> Benefits:
>
> - Type safety: token_info.role, token_info.agent_id
> - Validation: can't create invalid TokenInfo
> - Clear schema: required role, optional agent_id
> - IDE support: autocomplete and type checking
>
> Code already uses TokenRole enum, should extend to full typed model.

```
      34:
      35: # Token table: token -> {role: str, agent_id?: str}
      36: # In production, this would be a database lookup or external service
>>>   37: TOKEN_TABLE: dict[str, dict[str, str]] = {
>>>   38:     "human-token-123": {"role": "human"},
>>>   39:     "agent-token-abc": {"role": "agent", "agent_id": "agent-1"},
>>>   40: }
      41:
      42:
      43: class MCPRoutingMiddleware(BaseHTTPMiddleware):
   ...
     112:
     113:         # Determine role and routing
     114:         try:
>>>  115:             role = TokenRole(token_info["role"])
>>>  116:             agent_id = token_info.get("agent_id")
     117:
     118:             logger.info(f"Routing MCP request: role={role}, agent_id={agent_id}")
     119:
```

### `truncation-byte-calc.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/truncation-byte-calc.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/agent/event_renderer.py#L128-L132)

File: `adgn/src/adgn/agent/event_renderer.py` (L128-132)

> `_truncate_text` (line 132) reports truncated bytes as
> `len(s.encode('utf-8')) - len(raw)`. At this point `s` was just decoded from
> `raw` (line 131), so re-encoding it produces nearly the same byte count —
> the difference is at most a few bytes from `errors="replace"` substituting
> broken trailing bytes with U+FFFD. The message always says something like
> "truncated (+0 bytes)" or "+3 bytes" instead of the actual amount removed.
> The original byte length should be captured before truncation.

```
     125:     # Utility methods --------------------------------------------------------
     126:
     127:     def _truncate_text(self, s: str) -> str:
>>>  128:         raw = s.encode("utf-8", errors="replace")
>>>  129:         if len(raw) > self._max_bytes:
>>>  130:             raw = raw[: self._max_bytes]
>>>  131:             s = raw.decode("utf-8", errors="replace")
>>>  132:             s += f"\n… truncated (+{len(s.encode('utf-8')) - len(raw)} bytes)"
     133:         lines = s.splitlines()
     134:         if len(lines) > self._max_lines:
     135:             kept = lines[: self._max_lines]
```

### `unnecessary-wrapper-functions.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/unnecessary-wrapper-functions.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/llm/sysrw/openai_typing.py#L126-L169)

File: `adgn/src/adgn/llm/sysrw/openai_typing.py` (L126-128, L131-133, L159-169)

> Trivial wrapper functions that add no abstraction value. Only create wrapper
> functions when they add real abstraction (combine multiple operations),
> provide domain-specific naming clarity, or encapsulate complex logic.
>
> **Note:** dump_response_messages/dump_chat_messages/parse_tool_params are one-line wrappers around Pydantic methods

```
     123:     return TypeAdapter(list[ResponseOutputMessage]).validate_python(messages)
     124:
     125:
>>>  126: def dump_response_messages(messages: list[ResponseOutputMessage]) -> list[dict[str, Any]]:
>>>  127:     """Convert validated ResponseOutputMessage objects back to dict form."""
>>>  128:     return [msg.model_dump(by_alias=True) for msg in messages]
     129:
     130:
>>>  131: def dump_chat_messages(messages: list[ChatCompletionMessageParam]) -> list[dict[str, Any]]:
>>>  132:     """Convert ChatCompletionMessageParam objects to dict form."""
>>>  133:     return [TypeAdapter(dict[str, Any]).validate_python(msg) for msg in messages]
     134:
     135:
     136: def parse_chat_messages(messages: Any) -> list[ChatCompletionMessageParam] | None:
   ...
     156:     return TypeAdapter(Response).validate_python(response)
     157:
     158:
>>>  159: def parse_tool_params(params: dict[str, Any]) -> dict[str, Any]:
>>>  160:     """Parse and validate tool parameters.
>>>  161:
>>>  162:     Args:
>>>  163:         params: Tool parameters as dict. If you have a JSON string,
>>>  164:                 deserialize it first: parse_tool_params(json.loads(json_str))
>>>  165:
>>>  166:     Returns:
>>>  167:         Validated parameter dict.
>>>  168:     """
>>>  169:     return TypeAdapter(dict[str, Any]).validate_python(params)
     170:
     171:
     172: def parse_tools_list(tools: Any) -> list[dict[str, Any]]:
```

### `unnecessary-wrapper-functions.yaml` / `occ-1`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/unnecessary-wrapper-functions.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/agent/agent.py#L149-L269)

File: `adgn/src/adgn/agent/agent.py` (L149-160, L269)

> Trivial wrapper functions that add no abstraction value. Only create wrapper
> functions when they add real abstraction (combine multiple operations),
> provide domain-specific naming clarity, or encapsulate complex logic.
>
> **Note:** \_normalize_call_arguments accepts dict[str, Any] | str | None but dict case never occurs; defensive check for
> impossible case

```
     146:     return _make_error_result(reason or DEFAULT_ABORT_ERROR)
     147:
     148:
>>>  149: def _normalize_call_arguments(arguments: dict[str, Any] | str | None) -> str | None:
>>>  150:     """Normalize function call arguments to JSON string.
>>>  151:
>>>  152:     Args:
>>>  153:         arguments: Structured data (dict), pre-serialized JSON string, or None.
>>>  154:
>>>  155:     Returns:
>>>  156:         JSON string representation or None if arguments is None.
>>>  157:     """
>>>  158:     if arguments is None or isinstance(arguments, str):
>>>  159:         return arguments
>>>  160:     return json.dumps(arguments)
     161:
     162:
     163: def _call_tool_result_from_json(output: str) -> CallToolResult:
   ...
     266:     async def _handle_pending_tool_calls(self) -> None:
     267:         function_calls: list[FunctionCallItem] = list(self.pending_function_calls)
     268:         calls: list[tuple[FunctionCallItem, str | None]] = [
>>>  269:             (function_call, _normalize_call_arguments(function_call.arguments)) for function_call in function_calls
     270:         ]
     271:
     272:         local_result_map: dict[str, CallToolResult] = {
```

### `wrong-ephemeral-flag.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/wrong-ephemeral-flag.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/agent/runtime/container.py#L622-L629)

File: `adgn/src/adgn/agent/runtime/container.py` (L622, L628-629)

> Line 622 creates the runtime container with `ephemeral=True`, but line 629
> stores `self.runtime_ephemeral = False`. The comment on 628 says "Persist
> runtime ephemerality for status reporting (explicit)" — the value contradicts
> both the comment's intent and the actual container configuration. Status
> reports will incorrectly show the runtime as non-ephemeral.

```
     619:
     620:             # Runtime exec server (no host mounts)
     621:             runtime_image = resolve_runtime_image()
>>>  622:             opts = ContainerOptions(image=runtime_image, volumes=None, ephemeral=True)
     623:             runtime_server = make_runtime_server(opts)
     624:             # Ensure tool is exposed under expected name
     625:             tools = await runtime_server._tool_manager.list_tools()
     626:             assert RUNTIME_EXEC_TOOL_NAME in [t.name for t in tools]
     627:             await self._compositor.mount_inproc(RUNTIME_SERVER_NAME, runtime_server)
>>>  628:         # Persist runtime ephemerality for status reporting (explicit)
>>>  629:         self.runtime_ephemeral = False
     630:         # Notification hooks are managed by the compositor client notifications buffer
     631:
     632:     # Seatbelt is no longer intercepted/rewritten; respect provided specs.
```

### `yaml-loader-falsy-coercion.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/issues/yaml-loader-falsy-coercion.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-00/code/adgn/src/adgn/agent/presets.py#L35)

File: `adgn/src/adgn/agent/presets.py` (L35)

> \_load_yaml coerces any falsy YAML payload to {} before the type check (line 35:
> `data = yaml.safe_load(f) or {}`). This means non-mapping presets like [], 0, false, or None
> are silently treated as empty mappings, bypassing the isinstance(data, dict) check on line 36
> that should raise "preset must be a mapping". The `or {}` should be removed - let yaml.safe_load
> return whatever it returns, and let the isinstance check fail naturally for non-dict values.
> This hides malformed presets and causes downstream validation errors instead of clear early
> failures.

```
      32:
      33: def _load_yaml(path: Path) -> dict[str, JsonValue]:
      34:     with path.open("r", encoding="utf-8") as f:
>>>   35:         data = yaml.safe_load(f) or {}
      36:     if not isinstance(data, dict):
      37:         raise ValueError(f"preset must be a mapping: {path}")
      38:     return cast(dict[str, JsonValue], data)
```

## ducktape/2025-11-22-02 (21)

### `ambiguous-timestamp-field.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/ambiguous-timestamp-field.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/src/adgn/agent/approvals.py#L79-L189)

File: `adgn/src/adgn/agent/approvals.py` (L79-85, L167, L189)

> The ApprovalItem model has a `timestamp` field whose meaning is ambiguous:
>
> ```python
> class ApprovalItem(BaseModel):
>     """A single approval (pending or decided)."""
>     call_id: str
>     tool_call: ToolCall
>     status: ApprovalStatus
>     reason: str | None = None
>     timestamp: datetime  # What does this represent?
> ```
>
> Looking at usage patterns reveals inconsistent semantics:
>
> - **Pending approvals** (line 167): `timestamp=datetime.now()` - uses current time when building the list
> - **Decided approvals** (line 189): `timestamp=record.decision.decided_at` - uses the decision time
>
> The field name "timestamp" doesn't clarify what event it's timestamping:
>
> - Is it when the tool call was requested?
> - When the approval decision was made?
> - When the approval item was last updated?
>
> For decided approvals it's explicitly the decision time (`decided_at`), but for pending approvals it's just
> "now"
> which
> is actually neither the request time nor a decision time. This semantic inconsistency makes the field unclear
> and
> potentially misleading.
>
> **Fix:**
> Rename to be more specific about what is being timestamped. Options include:
>
> - `updated_at` - if it represents last update time for both states
> - Split into `requested_at` and `decided_at` fields where decided_at is nullable
> - Use a union type with status-specific semantics
>
> The name should make it clear what temporal event is being recorded, and the semantics should be consistent
> across
> both
> pending and decided states.

```
      76:     ABORTED = "aborted"
      77:
      78:
>>>   79: class ApprovalItem(BaseModel):
>>>   80:     """A single approval (pending or decided)."""
>>>   81:     call_id: str
>>>   82:     tool_call: ToolCall
>>>   83:     status: ApprovalStatus
>>>   84:     reason: str | None = None
>>>   85:     timestamp: datetime
      86:
      87:
      88: class ApprovalsResponse(BaseModel):
   ...
     164:                     tool_call=tool_call,
     165:                     status=ApprovalStatus.PENDING,
     166:                     reason=None,
>>>  167:                     timestamp=datetime.now(),  # Approx timestamp for pending
     168:                 )
     169:                 for call_id, tool_call in pending_map.items()
     170:             ]
   ...
     186:                     tool_call=record.tool_call,
     187:                     status=map_outcome_to_status(record.decision.outcome),
     188:                     reason=record.decision.reason,
>>>  189:                     timestamp=record.decision.decided_at,
     190:                 )
     191:                 for record in records
     192:                 if record.decision is not None
```

### `docker-check-at-call-sites.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/docker-check-at-call-sites.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/src/adgn/agent/approvals.py#L344-L361)

File: `adgn/src/adgn/agent/approvals.py` (L344-345, L360-361)

> The pattern `if self.docker_client is not None: self.self_check(...)` appears
> twice (lines 344-345, 360-361). This conditional is repeated at every call site.
>
> The check should be internal to self_check() itself, not the caller's
> responsibility. Currently self_check() assumes docker_client is valid (line 342),
> forcing callers to guard it.
>
> Fix: Move the None check inside self_check():
>
> def self_check(self, source: str) -> None:
> if self.docker_client is None:
> return # Skip validation if Docker not available
> run_policy_source(docker_client=self.docker_client, ...)
>
> Then call sites simplify to: self.self_check(content)
>
> Benefits:
>
> - Single responsibility: self_check handles its own preconditions
> - DRY: check not repeated at call sites
> - Cleaner API: callers don't need to know about Docker availability

```
     341:         persists it, and notifies about the change.
     342:         """
     343:         # Self-check proposal program if docker is available
>>>  344:         if self.docker_client is not None:
>>>  345:             self.self_check(content)
     346:         # Create proposal and get actual database-assigned ID
     347:         new_id = await self.persistence.create_policy_proposal(self.agent_id, proposal_id=0, content=content)
     348:         await self.notify_proposal_change(new_id)
   ...
     357:         if (got := await self.persistence.get_policy_proposal(self.agent_id, proposal_id)) is None:
     358:             raise KeyError(str(proposal_id))
     359:         # Self-check the proposal program before activation
>>>  360:         if self.docker_client is not None:
>>>  361:             self.self_check(got.content)
     362:         # Activate policy (notifies via engine's set_policy)
     363:         await self.set_policy(got.content)
     364:         await self.persistence.approve_policy_proposal(self.agent_id, proposal_id)
```

### `duplicate-bearer-extraction.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/duplicate-bearer-extraction.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/src/adgn/agent/mcp_bridge/auth.py#L75-L161)

File: `adgn/src/adgn/agent/mcp_bridge/auth.py` (L75-91, L144-161)

> Both TokenAuthMiddleware and UITokenAuthMiddleware duplicate the same
> Bearer token extraction logic:
>
> TokenAuthMiddleware.dispatch() (lines 75-91):
>
> - Check if Authorization header exists
> - Split on whitespace
> - Validate format is "Bearer <token>"
> - Extract the token (parts[1])
>
> UITokenAuthMiddleware.**call**() (lines 144-161):
>
> - Same exact pattern with slightly different error handling
>
> This is classic code duplication. Both implementations:
>
> 1. Check if Authorization header exists
> 2. Split on whitespace
> 3. Validate format is "Bearer <token>"
> 4. Extract the token (parts[1])
>
> Fix options:
>
> 1. Extract a shared helper function: extract_bearer_token(auth_header)
>    that returns (token | None, error_dict | None)
> 2. Preferred: Use FastMCP's authentication patterns if available
>    (investigate if FastMCP provides built-in Bearer token middleware,
>    authentication dependency injection, or standard auth utilities)
> 3. Consolidate middleware: if both are doing the same thing (Bearer
>    token validation), consider a single parameterized middleware:
>    BearerTokenMiddleware(token_validator: Callable)
>
> Most modern Python web frameworks (FastAPI, Starlette, etc.) provide
> standardized auth patterns. If FastMCP builds on these, use the provided
> patterns instead of rolling custom middleware.
>
> This eliminates the duplication entirely by extracting or unifying the
> two use cases.

```
      72:         self.token_mapping = token_mapping
      73:
      74:     async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
>>>   75:         auth_header = request.headers.get("Authorization")
>>>   76:         if not auth_header:
>>>   77:             raise HTTPException(
>>>   78:                 status_code=status.HTTP_401_UNAUTHORIZED,
>>>   79:                 detail="Missing Authorization header",
>>>   80:                 headers={"WWW-Authenticate": "Bearer"},
>>>   81:             )
>>>   82:
>>>   83:         parts = auth_header.split()
>>>   84:         if len(parts) != 2 or parts[0].lower() != "bearer":
>>>   85:             raise HTTPException(
>>>   86:                 status_code=status.HTTP_401_UNAUTHORIZED,
>>>   87:                 detail="Invalid Authorization header format (expected: Bearer <token>)",
>>>   88:                 headers={"WWW-Authenticate": "Bearer"},
>>>   89:             )
>>>   90:
>>>   91:         token = parts[1]
      92:
      93:         if (agent_id := self.token_mapping.get_agent_id(token)) is None:
      94:             raise HTTPException(
   ...
     141:
     142:         # Parse headers
     143:         headers = dict(scope.get("headers", []))
>>>  144:         auth_header = headers.get(b"authorization", b"").decode()
>>>  145:
>>>  146:         # Validate authentication
>>>  147:         error_response = None
>>>  148:         if not auth_header:
>>>  149:             error_response = self._create_error_response(
>>>  150:                 401, "Missing Authorization header"
>>>  151:             )
>>>  152:         else:
>>>  153:             parts = auth_header.split()
>>>  154:             if len(parts) != 2 or parts[0].lower() != "bearer":
>>>  155:                 error_response = self._create_error_response(
>>>  156:                     401, "Invalid Authorization header format (expected: Bearer <token>)"
>>>  157:                 )
>>>  158:             elif parts[1] != self.expected_token:
>>>  159:                 error_response = self._create_error_response(
>>>  160:                     401, "Invalid token"
>>>  161:                 )
     162:
     163:         # Send error or continue
     164:         if error_response:
```

### `duplicated-agent-lookup.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/duplicated-agent-lookup.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/src/adgn/agent/mcp_bridge/server.py#L158-L237)

File: `adgn/src/adgn/agent/mcp_bridge/server.py` (L158-165, L168-177, L224-237)

> Methods `get_agent_mode` (lines 168-177), `get_infrastructure` (lines 158-165), and
> `remove_agent` (lines 224-237) duplicate the same "get agent or raise KeyError" logic.
>
> Each method: (1) Checks if agent_id in self.\_agents. (2) Gets self.\_agents[agent_id].agent.
> (3) Checks if agent is None. (4) Raises KeyError with similar messages. Only difference is
> what field they return (agent.mode vs agent.running) or what they do with the agent.
>
> Classic code duplication. Extract common helper `_get_agent_or_raise(agent_id) -> RunningAgent`
> that consolidates the lookup logic and raises KeyError if not found/initialized. Then simplify
> all callers to one-liners: `return self._get_agent_or_raise(agent_id).mode`,
> `return self._get_agent_or_raise(agent_id).running`, etc.
>
> Benefits: DRY - single implementation of lookup logic, consistent error messages, easier to
> maintain. Could even inline some one-liners if called in few places.

```
     155:     async def get_compositor_app(self, agent_id: AgentID) -> FastAPI:
     156:         """Get compositor app for an agent_id."""
     157:         _, app = await self.get_or_create_infrastructure(agent_id)
>>>  158:         return app
>>>  159:
>>>  160:     def get_running_infrastructure(self, agent_id: AgentID) -> RunningInfrastructure | None:
>>>  161:         """Get running infrastructure if it exists (doesn't create)."""
>>>  162:         entry = self._agents.get(agent_id)
>>>  163:         return entry.agent.running if entry and entry.agent else None
>>>  164:
>>>  165:     def known_agents(self) -> list[AgentID]:
     166:         return list(self._agents.keys())
     167:
>>>  168:     async def get_infrastructure(self, agent_id: AgentID) -> RunningInfrastructure:
>>>  169:         """Raises KeyError if agent not in registry or not yet initialized."""
>>>  170:         if agent_id not in self._agents:
>>>  171:             raise KeyError(f"Agent {agent_id} not found in registry")
>>>  172:         agent = self._agents[agent_id].agent
>>>  173:         if agent is None:
>>>  174:             raise KeyError(f"Agent {agent_id} infrastructure not yet initialized")
>>>  175:         return agent.running
>>>  176:
>>>  177:     def get_agent_mode(self, agent_id: AgentID) -> AgentMode:
     178:         """Raises KeyError if agent not in registry or not yet initialized."""
     179:         if agent_id not in self._agents:
     180:             raise KeyError(f"Agent {agent_id} not found in registry")
   ...
     221:         running, _ = await self.get_or_create_infrastructure(agent_id)
     222:         return running
     223:
>>>  224:     async def remove_agent(self, agent_id: AgentID) -> None:
>>>  225:         """Remove and clean up agent infrastructure.
>>>  226:
>>>  227:         Closes the running infrastructure and removes the agent from the registry.
>>>  228:         """
>>>  229:         if agent_id not in self._agents:
>>>  230:             raise KeyError(f"Agent {agent_id} not found in registry")
>>>  231:
>>>  232:         agent = self._agents[agent_id].agent
>>>  233:         if agent is not None:
>>>  234:             await agent.running.close()
>>>  235:
>>>  236:         del self._agents[agent_id]
>>>  237:
     238:         await self.notify_agents_list_changed()
     239:
     240:     def _register_resources(self) -> None:
```

### `duplicated-get-proposal-check.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/duplicated-get-proposal-check.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/src/adgn/agent/approvals.py#L357-L401)

File: `adgn/src/adgn/agent/approvals.py` (L357-358, L399-401)

> The "get proposal or raise KeyError if None" pattern appears twice:
>
> Lines 357-358 (approve_proposal):
> if (got := await self.persistence.get_policy_proposal(...)) is None:
> raise KeyError(str(proposal_id))
>
> Lines 399-401 (proposal_detail):
> got = await self.persistence.get_policy_proposal(...)
> if got is None:
> raise KeyError(f"Proposal {id} not found")
>
> This is code duplication. Both:
>
> 1. Call get_policy_proposal()
> 2. Check if result is None
> 3. Raise KeyError with the proposal ID
>
> The "get or None" version (get_policy_proposal) might not be used anywhere
> without this immediate None check. If that's the case, the persistence
> method itself should raise.
>
> Fix options:
>
> 1. Preferred: Add get_policy_proposal_or_raise() to persistence layer that
>    raises KeyError instead of returning None
> 2. Alternative: Add local helper method \_get_proposal_or_raise()
> 3. Check if nullable version is actually needed - if never called without
>    the None check, delete it and make the main method raise
>
> This simplifies call sites to: got = await persistence.get_policy_proposal_or_raise(...)

```
     354:         Retrieves the proposal, validates it, activates it as the current policy,
     355:         marks it approved in persistence, and notifies about the change.
     356:         """
>>>  357:         if (got := await self.persistence.get_policy_proposal(self.agent_id, proposal_id)) is None:
>>>  358:             raise KeyError(str(proposal_id))
     359:         # Self-check the proposal program before activation
     360:         if self.docker_client is not None:
     361:             self.self_check(got.content)
   ...
     396:         @self.resource("resource://proposals/{id}", name="proposal_detail", mime_type="application/json")
     397:         async def proposal_detail(id: str) -> ProposalDetail:
     398:             """Get full proposal details including content and metadata."""
>>>  399:             got = await self.persistence.get_policy_proposal(self.agent_id, id)
>>>  400:             if got is None:
>>>  401:                 raise KeyError(f"Proposal {id} not found")
     402:
     403:             return ProposalDetail(
     404:                 id=got.id,
```

### `keyerror-iteration-mismatch.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/keyerror-iteration-mismatch.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/src/adgn/agent/mcp_bridge/server.py#L245-L249)

File: `adgn/src/adgn/agent/mcp_bridge/server.py` (L245-249)

> Iteration catches KeyError when agent isn't initialized (lines 245-249):
>
> ```python
> for agent_id in self.known_agents():
>     try:
>         mode = self.get_agent_mode(agent_id)
>     except KeyError:
>         continue
> ```
>
> This is a code smell indicating poorly structured iteration. We iterate over
> `known_agents()` (returns ALL agent IDs), then call `get_agent_mode()` which
> raises KeyError for uninitialized agents. The mismatch between iteration source
> and accessed data forces the try/except.
>
> Should iterate over a structure where agent mode is guaranteed to exist:
>
> ```python
> for agent_id, entry in self._agents.items():
>     if entry.agent is None:
>         continue  # Skip uninitialized agents
>     agent = entry.agent
>     infra = agent.running
>     # ... rest of logic with guaranteed agent data
> ```
>
> Or explicitly decide whether to include uninitialized agents with different status.

```
     242:         async def list_agents() -> AgentsListResponse:
     243:             """List all agents with detailed status."""
     244:             agents = []
>>>  245:             for agent_id in self.known_agents():
>>>  246:                 try:
>>>  247:                     mode = self.get_agent_mode(agent_id)
>>>  248:                 except KeyError:
>>>  249:                     continue
     250:
     251:                 # Get infrastructure if available
     252:                 infra = self.get_running_infrastructure(agent_id)
```

### `missing-call-id-silent-fail.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/missing-call-id-silent-fail.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/src/adgn/agent/approvals.py#L131-L148)

File: `adgn/src/adgn/agent/approvals.py` (L142-148, L131-137)

> Both `resolve()` and `await_decision()` silently handle missing call_ids instead of failing fast.
>
> Problem 1 (lines 142-148): `resolve()` uses `pop(call_id, None)` which swallows missing call_ids AND still
> sends
> notification even though nothing changed. Should use direct dict access (`self._pending[call_id]`) to raise
> KeyError
> on
> missing entries.
>
> Problem 2 (lines 131-137): `await_decision()` uses `.get(call_id)` and auto-creates new pending approval if
> missing.
> Unclear if intentional for first-time calls or if it should raise on truly missing entries.
>
> Use direct dict access to surface errors immediately rather than silently swallowing them. Only notify when
> state
> actually changes.

```
     128:     async def await_decision(
     129:         self, call_id: str, tool_call: ToolCall
     130:     ) -> ContinueDecision | DenyContinueDecision | AbortTurnDecision:
>>>  131:         async with self._lock:
>>>  132:             pending = self._pending.get(call_id)
>>>  133:             if pending is None:
>>>  134:                 fut = asyncio.get_running_loop().create_future()
>>>  135:                 self._pending[call_id] = PendingApproval(tool_call=tool_call, future=fut)
>>>  136:             else:
>>>  137:                 fut = pending.future
     138:         if self._has_mcp:
     139:             await self.notify_approvals_changed()
     140:         return await fut
     141:
>>>  142:     def resolve(self, call_id: str, decision: ContinueDecision | DenyContinueDecision | AbortTurnDecision) -> None:
>>>  143:         pending = self._pending.pop(call_id, None)
>>>  144:         if pending is not None and not pending.future.done():
>>>  145:             pending.future.set_result(decision)
>>>  146:         # Schedule notification asynchronously if MCP is enabled
>>>  147:         if self._has_mcp:
>>>  148:             asyncio.create_task(self.notify_approvals_changed())
     149:
     150:     @property
     151:     def pending(self) -> dict[str, ToolCall]:
```

### `redundant-function-call-param.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/redundant-function-call-param.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/src/adgn/agent/agent.py#L305)

File: `adgn/src/adgn/agent/agent.py` (L305)

> The invoker callback is called with both a FunctionCall object and its arguments as separate parameters:
>
> ```python
> outcome = await invoker(fc, fc.arguments)
> ```
>
> The second parameter `fc.arguments` is redundant because it can be trivially derived from the first parameter
> (fc.arguments). This violates DRY - the invoker should only need the FunctionCall object.
>
> This is essentially a form of unnecessary aliasing/renaming where the caller is extracting a field and passing
> it
> separately, forcing the callee to receive the same information twice. The invoker implementation should
> extract
> arguments internally when needed.
>
> **Fix:**
> Change the invoker signature to accept only the FunctionCall object:
>
> ```python
> outcome = await invoker(fc)
> ```
>
> Update the invoker implementation to extract arguments internally:
>
> ```python
> async def invoker(fc: FunctionCall) -> Outcome:
>     arguments = fc.arguments
>     # ... rest of logic
> ```
>
> This removes the redundant parameter and makes the API cleaner by avoiding unnecessary data extraction at the
> call
> site.

```
     302:             async def runner(fc: FunctionCallItem) -> None:
     303:                 nonlocal abort_triggered
     304:                 try:
>>>  305:                     outcome = await invoker(fc, fc.arguments)
     306:                 except cancelled_exc:
     307:                     return
     308:                 cid = _require_call_id(fc)
```

### `redundant-mode-field-derived.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/redundant-mode-field-derived.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/src/adgn/agent/mcp_bridge/server.py#L40)

File: `adgn/src/adgn/agent/mcp_bridge/server.py` (L40)

> Line 40 defines `RunningAgent` dataclass with both `mode: AgentMode` and
> `local_runtime: LocalAgentRuntime | None` fields. The mode is completely determined by
> whether local_runtime exists: `mode = BRIDGE` when `local_runtime = None`,
> `mode = LOCAL` when `local_runtime is not None`.
>
> This is redundant storage. Mode should be derived from local_runtime presence, not stored
> separately. Storing both creates risk of inconsistency (can't get out of sync if mode is
> computed).
>
> Replace the `mode` field with a property that returns `AgentMode.LOCAL if self.local_runtime
else AgentMode.BRIDGE`. Update construction sites to omit the mode parameter. Benefits:
> single source of truth, cannot desync, less data to maintain, clear semantic relationship.

```
      37: @dataclass
      38: class RunningAgent:
      39:     """All infrastructure for a running agent (single point of optionality)."""
>>>   40:
      41:     running: RunningInfrastructure
      42:     compositor_app: FastAPI
      43:     mode: AgentMode
```

### `redundant-status-conversion.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/redundant-status-conversion.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/src/adgn/agent/approvals.py#L388-L405)

File: `adgn/src/adgn/agent/approvals.py` (L388, L405)

> Both resource handlers convert p.status and got.status to ProposalStatus:
>
> Line 388: status=ProposalStatus(p.status)
> Line 405: status=ProposalStatus(got.status)
>
> This conversion is necessarily redundant:
>
> Case 1: If p.status and got.status are already ProposalStatus, then
> ProposalStatus(p.status) is a no-op that should be p.status directly.
>
> Case 2: If p.status is a different type (e.g., string or database enum),
> this indicates a type inconsistency that should be fixed upstream.
>
> Similar to finding 024 (ApprovalOutcome vs ApprovalStatus), this suggests
> ProposalStatus might have a duplicate in the persistence layer, requiring
> conversion at the boundary.
>
> Fix options:
>
> 1. If already ProposalStatus: remove conversion, use status=p.status
> 2. If persistence returns different type: unify types - make persistence
>    return ProposalStatus directly, OR move conversion into persistence
>    layer's model so it returns objects with ProposalStatus already set
> 3. Most likely: duplicate enums that should be unified
>
> This is a type correctness issue - types should match at boundaries
> without runtime conversion.

```
     385:                 proposals=[
     386:                     ProposalDescriptor(
     387:                         id=p.id,
>>>  388:                         status=ProposalStatus(p.status),
     389:                         created_at=p.created_at,
     390:                         decided_at=p.decided_at,
     391:                     )
   ...
     402:
     403:             return ProposalDetail(
     404:                 id=got.id,
>>>  405:                 status=ProposalStatus(got.status),
     406:                 created_at=got.created_at,
     407:                 decided_at=got.decided_at,
     408:                 content=got.content,
```

### `redundant-total-tokens-field.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/redundant-total-tokens-field.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/src/adgn/agent/handler.py#L29)

File: `adgn/src/adgn/agent/handler.py` (L29)

> The TokenUsage model has a total_tokens field that is a trivial sum of two other fields:
>
> class TokenUsage(BaseModel):
> input_tokens: int | None = Field(None, ...)
> output_tokens: int | None = Field(None, ...)
> total_tokens: int | None = Field(None, description="Total tokens consumed (input + output)")
>
> The total_tokens field is redundant:
>
> - It's always input_tokens + output_tokens
> - No additional information
> - Must be kept in sync manually (error-prone)
> - Wastes storage/bandwidth
>
> This violates DRY - the total is trivially computable from the parts.
>
> Fix options:
>
> 1. Preferred: Remove total_tokens field entirely. Callers compute:
>    total = (usage.input_tokens or 0) + (usage.output_tokens or 0)
> 2. For API compatibility, make it a computed property:
>    @property
>    def total_tokens(self) -> int | None:
>    if self.input_tokens is None and self.output_tokens is None:
>    return None
>    return (self.input_tokens or 0) + (self.output_tokens or 0)
>
> This ensures:
>
> - Single source of truth (input + output)
> - Cannot get out of sync
> - No redundant storage
> - Backward compatible if needed

```
      26:     input_tokens_details: InputTokensDetails | None = Field(None, description="Breakdown of input token usage")
      27:     output_tokens: int | None = Field(None, description="Number of output tokens generated")
      28:     output_tokens_details: OutputTokensDetails | None = Field(None, description="Breakdown of output token usage")
>>>   29:     total_tokens: int | None = Field(None, description="Total tokens consumed (input + output)")
      30:
      31:
      32: # ---- Typed events (no shared runtime base required) ----
```

### `split-with-ui-conditional.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/split-with-ui-conditional.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/src/adgn/agent/runtime/builder.py#L70-L86)

File: `adgn/src/adgn/agent/runtime/builder.py` (L70-72, L84-86)

> Lines 70-72 and 84-86 split with_ui conditional logic unnecessarily. First
> block creates ui_bus and connection_manager, then builder.start() executes,
> then second block attaches UI sidecar. These operations are independent and
> could be consolidated.
>
> **Problem:** Split conditional increases cognitive load and makes control flow
> harder to follow. The two if with_ui blocks could be merged, or consolidated
> entirely by moving ConnectionManager construction inline and creating ui_bus
> only when needed.
>
> **Fix:** Consolidate into single block after builder.start() by using inline
> conditional for connection_manager and creating ui_bus only in the final if
> block. Eliminates split conditional.

```
      67:     """
      68:     ui_bus: ServerBus | None = None
      69:     connection_manager: ConnectionManager | None = None
>>>   70:     if with_ui:
>>>   71:         ui_bus = ServerBus()
>>>   72:         connection_manager = ConnectionManager()
      73:
      74:     builder = MCPInfrastructure(
      75:         agent_id=agent_id,
   ...
      81:
      82:     running = await builder.start(mcp_config)
      83:
>>>   84:     if with_ui:
>>>   85:         assert ui_bus is not None
>>>   86:         await running.attach_sidecar(UISidecar(ui_bus))
      87:     await running.attach_sidecar(ChatSidecar())
      88:     await running.attach_sidecar(LoopControlSidecar())
      89:
```

### `swallow-initialization-error.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/swallow-initialization-error.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/src/adgn/agent/mcp_bridge/compositor_factory.py#L93-L95)

File: `adgn/src/adgn/agent/mcp_bridge/compositor_factory.py` (L93-95)

> Lines 93-95 catch exceptions when mounting agent compositors and continue
> silently with logged error. This is dangerous initialization behavior.
>
> **Why this is wrong:**
>
> 1. Silent failure: server starts but missing critical infrastructure
> 2. Inconsistent state: some agents mounted, others missing
> 3. No recovery path: failed agent is simply absent forever
> 4. Violates fail-fast: better to crash loudly than fail silently
> 5. Debugging nightmare: errors logged but system appears "healthy"
>
> **Mounting compositors is critical infrastructure.** If it fails, the server
> is misconfigured and should not start.
>
> **Fix:** Remove try/except entirely. Let exception propagate so server crashes
> during startup, operator sees error immediately, and system never enters
> partially-broken state. Initialization failures should crash.
>
> If partial mounting is truly needed (unlikely), requires explicit tracking,
> health checks, error APIs, recovery logic, and documentation.

```
      90:             agent_comp = await create_agent_compositor(agent_id, registry)
      91:             await global_comp.mount_inproc(f"agent{agent_id}", agent_comp)
      92:             logger.info(f"Mounted agent compositor for agent {agent_id}")
>>>   93:         except Exception as e:
>>>   94:             logger.error(f"Failed to mount compositor for agent {agent_id}: {e}", exc_info=True)
>>>   95:             # Continue mounting other agents
      96:
      97:     # Standard infrastructure (resources aggregator, compositor metadata, admin)
      98:     if gateway_client is not None:
```

### `ternary-oneliner-needed.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/ternary-oneliner-needed.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/src/adgn/agent/mcp_bridge/cli.py#L88-L90)

File: `adgn/src/adgn/agent/mcp_bridge/cli.py` (L88-90)

> The policy_source initialization uses two lines when it could be a single ternary expression:
>
> ```python
> policy_source = None
> if initial_policy:
>     policy_source = initial_policy.read_text()
> ```
>
> This is a simple conditional assignment - perfect for a ternary operator.
>
> Replace with ternary oneliner:
>
> ```python
> policy_source = initial_policy.read_text() if initial_policy else None
> ```
>
> Benefits:
>
> - More concise (one line vs three)
> - Standard Python idiom for conditional assignment
> - Clearer intent (assigning based on condition)
> - Variable is const-assigned (not mutated)

```
      85:     else:
      86:         config = MCPConfig(mcpServers={})
      87:
>>>   88:     policy_source = None
>>>   89:     if initial_policy:
>>>   90:         policy_source = initial_policy.read_text()
      91:
      92:     asyncio.run(
      93:         _run_server(
```

### `test-dup-responses-create.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/test-dup-responses-create.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/tests/agent/e2e/test_mcp_concurrent.py#L100-L283)

File: `adgn/tests/agent/e2e/test_mcp_concurrent.py` (L100-110, L159-169, L269-283)

> The pattern of creating stateful mock response handlers (a dict with `{"i": 0}` and an
> `async def responses_create(_req)` function that increments the counter and returns
> tool calls from a sequence) is duplicated 16+ times across the test suite.
>
> **Fix:** Extract into a shared `make_stateful_responses(responses_factory, response_sequence)`
> helper in conftest.py or tests/agent/helpers.py that takes a list of (function_name,
> server_name, params) tuples and returns the stateful handler. This eliminates duplication
> across all 16+ instances.
>
> **Note:** Three instances in test_mcp_concurrent.py

```
      97:     - Resubscribe
      98:     - State consistency maintained
      99:     """
>>>  100:     state = {"i": 0}
>>>  101:
>>>  102:     async def responses_create(_req):
>>>  103:         i = state["i"]
>>>  104:         state["i"] = i + 1
>>>  105:         if i == 0:
>>>  106:             return responses_factory.make_tool_call(
>>>  107:                 build_mcp_function("echo", "echo"), {"text": "first call"}, call_id="call_echo_1"
>>>  108:             )
>>>  109:         return responses_factory.make_tool_call(build_mcp_function("ui", "end_turn"), {}, call_id="call_ui_end")
>>>  110:
     111:     s = run_server(lambda model: make_mock(responses_create))
     112:     base = s["base_url"]
     113:
   ...
     156:     - No errors occur
     157:     - Graceful cleanup
     158:     """
>>>  159:     state = {"i": 0}
>>>  160:
>>>  161:     async def responses_create(_req):
>>>  162:         i = state["i"]
>>>  163:         state["i"] = i + 1
>>>  164:         if i == 0:
>>>  165:             return responses_factory.make_tool_call(
>>>  166:                 build_mcp_function("echo", "echo"), {"text": "test"}, call_id="call_echo_1"
>>>  167:             )
>>>  168:         return responses_factory.make_tool_call(build_mcp_function("ui", "end_turn"), {}, call_id="call_ui_end")
>>>  169:
     170:     s = run_server(lambda model: make_mock(responses_create))
     171:     base = s["base_url"]
     172:
   ...
     266:     - Simulate network hiccup (pause/resume via offline mode)
     267:     - Subscription recovers correctly
     268:     """
>>>  269:     state = {"i": 0}
>>>  270:
>>>  271:     async def responses_create(_req):
>>>  272:         i = state["i"]
>>>  273:         state["i"] = i + 1
>>>  274:         if i == 0:
>>>  275:             return responses_factory.make_tool_call(
>>>  276:                 build_mcp_function("echo", "echo"), {"text": "before disconnect"}, call_id="call_echo_1"
>>>  277:             )
>>>  278:         if i == 1:
>>>  279:             return responses_factory.make_tool_call(
>>>  280:                 build_mcp_function("echo", "echo"), {"text": "after disconnect"}, call_id="call_echo_2"
>>>  281:             )
>>>  282:         return responses_factory.make_tool_call(build_mcp_function("ui", "end_turn"), {}, call_id="call_ui_end")
>>>  283:
     284:     s = run_server(lambda model: make_mock(responses_create))
     285:     base = s["base_url"]
     286:
```

### `test-dup-responses-create.yaml` / `occ-1`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/test-dup-responses-create.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/tests/agent/e2e/test_mcp_errors.py#L73-L256)

File: `adgn/tests/agent/e2e/test_mcp_errors.py` (L73-82, L127-135, L184-193, L249-256)

> The pattern of creating stateful mock response handlers (a dict with `{"i": 0}` and an
> `async def responses_create(_req)` function that increments the counter and returns
> tool calls from a sequence) is duplicated 16+ times across the test suite.
>
> **Fix:** Extract into a shared `make_stateful_responses(responses_factory, response_sequence)`
> helper in conftest.py or tests/agent/helpers.py that takes a list of (function_name,
> server_name, params) tuples and returns the stateful handler. This eliminates duplication
> across all 16+ instances.
>
> **Note:** Four instances in test_mcp_errors.py

```
      70:     """
      71:     state = {"i": 0}
      72:
>>>   73:     async def responses_create(_req):
>>>   74:         i = state["i"]
>>>   75:         state["i"] = i + 1
>>>   76:         if i == 0:
>>>   77:             # First call: try to use a tool from the broken server
>>>   78:             return responses_factory.make_tool_call(
>>>   79:                 build_mcp_function("broken", "broken_tool"), {"trigger": "break"}, call_id="call_broken_1"
>>>   80:             )
>>>   81:         # Second call: end turn
>>>   82:         return responses_factory.make_tool_call(build_mcp_function("ui", "end_turn"), {}, call_id="call_ui_end")
      83:
      84:     s = run_server(lambda model: make_mock(responses_create))
      85:     base = s["base_url"]
   ...
     124:     """
     125:     state = {"i": 0}
     126:
>>>  127:     async def responses_create(_req):
>>>  128:         i = state["i"]
>>>  129:         state["i"] = i + 1
>>>  130:         if i == 0:
>>>  131:             # Try to call the slow tool with a reasonable delay
>>>  132:             return responses_factory.make_tool_call(
>>>  133:                 build_mcp_function("slow", "slow_tool"), {"delay_seconds": 2}, call_id="call_slow_1"
>>>  134:             )
>>>  135:         return responses_factory.make_tool_call(build_mcp_function("ui", "end_turn"), {}, call_id="call_ui_end")
     136:
     137:     s = run_server(lambda model: make_mock(responses_create))
     138:     base = s["base_url"]
   ...
     181:     # Use a simple echo server that we can "kill" by having it fail
     182:     state = {"i": 0, "should_fail": False}
     183:
>>>  184:     async def responses_create(_req):
>>>  185:         i = state["i"]
>>>  186:         state["i"] = i + 1
>>>  187:         if i == 0:
>>>  188:             # First call: use echo tool
>>>  189:             return responses_factory.make_tool_call(
>>>  190:                 build_mcp_function("echo", "echo"), {"text": "test message"}, call_id="call_echo_1"
>>>  191:             )
>>>  192:         # Subsequent calls: end turn
>>>  193:         return responses_factory.make_tool_call(build_mcp_function("ui", "end_turn"), {}, call_id="call_ui_end")
     194:
     195:     s = run_server(lambda model: make_mock(responses_create))
     196:     base = s["base_url"]
   ...
     246:     """
     247:     state = {"i": 0}
     248:
>>>  249:     async def responses_create(_req):
>>>  250:         i = state["i"]
>>>  251:         state["i"] = i + 1
>>>  252:         if i == 0:
>>>  253:             return responses_factory.make_tool_call(
>>>  254:                 build_mcp_function("echo", "echo"), {"text": "test"}, call_id="call_echo_1"
>>>  255:             )
>>>  256:         return responses_factory.make_tool_call(build_mcp_function("ui", "end_turn"), {}, call_id="call_ui_end")
     257:
     258:     s = run_server(lambda model: make_mock(responses_create))
     259:     base = s["base_url"]
```

### `test-dup-responses-create.yaml` / `occ-2`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/test-dup-responses-create.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/tests/agent/e2e/test_mcp_edge_cases.py#L38-L283)

File: `adgn/tests/agent/e2e/test_mcp_edge_cases.py` (L38-51, L100-101, L139-152, L207-220, L275-283)

> The pattern of creating stateful mock response handlers (a dict with `{"i": 0}` and an
> `async def responses_create(_req)` function that increments the counter and returns
> tool calls from a sequence) is duplicated 16+ times across the test suite.
>
> **Fix:** Extract into a shared `make_stateful_responses(responses_factory, response_sequence)`
> helper in conftest.py or tests/agent/helpers.py that takes a list of (function_name,
> server_name, params) tuples and returns the stateful handler. This eliminates duplication
> across all 16+ instances.
>
> **Note:** Five instances in test_mcp_edge_cases.py

```
      35:     - UI displays error state without crashing
      36:     - Agent continues to function after the error
      37:     """
>>>   38:     state = {"i": 0}
>>>   39:
>>>   40:     async def responses_create(_req):
>>>   41:         i = state["i"]
>>>   42:         state["i"] = i + 1
>>>   43:         if i == 0:
>>>   44:             # Try to subscribe to invalid resource
>>>   45:             return responses_factory.make_tool_call(
>>>   46:                 build_mcp_function("resources", "subscribe"),
>>>   47:                 {"server": "test_server", "uri": "resource://invalid/nonexistent"},
>>>   48:                 call_id="call_invalid_sub",
>>>   49:             )
>>>   50:         # End turn
>>>   51:         return responses_factory.make_tool_call(build_mcp_function("ui", "end_turn"), {}, call_id="call_ui_end")
      52:
      53:     s = run_server(lambda model: make_mock(responses_create))
      54:     base = s["base_url"]
   ...
      97:     - System handles rapid lifecycle transitions gracefully
      98:     """
      99:
>>>  100:     async def responses_create(_req):
>>>  101:         return responses_factory.make_assistant_message("ok")
     102:
     103:     s = run_server(lambda model: make_mock(responses_create))
     104:     base = s["base_url"]
   ...
     136:     - Verify subscription state is handled gracefully
     137:     - UI reflects current server state
     138:     """
>>>  139:     state = {"i": 0}
>>>  140:
>>>  141:     async def responses_create(_req):
>>>  142:         i = state["i"]
>>>  143:         state["i"] = i + 1
>>>  144:         if i == 0:
>>>  145:             # Subscribe to a resource
>>>  146:             return responses_factory.make_tool_call(
>>>  147:                 build_mcp_function("resources", "subscribe"),
>>>  148:                 {"server": "echo", "uri": "resource://test/data"},
>>>  149:                 call_id="call_sub_1",
>>>  150:             )
>>>  151:         # End turn
>>>  152:         return responses_factory.make_tool_call(build_mcp_function("ui", "end_turn"), {}, call_id="call_ui_end")
     153:
     154:     s = run_server(lambda model: make_mock(responses_create))
     155:     base = s["base_url"]
   ...
     204:
     205:     Note: This test simulates the condition by using a slow/hanging resource.
     206:     """
>>>  207:     state = {"i": 0}
>>>  208:
>>>  209:     async def responses_create(_req):
>>>  210:         i = state["i"]
>>>  211:         state["i"] = i + 1
>>>  212:         if i == 0:
>>>  213:             # Try to read a resource that will timeout/hang
>>>  214:             return responses_factory.make_tool_call(
>>>  215:                 build_mcp_function("resources", "read"),
>>>  216:                 {"server": "slow_server", "uri": "resource://slow/data", "start_offset": 0, "max_bytes": 1024},
>>>  217:                 call_id="call_read_slow",
>>>  218:             )
>>>  219:         # End turn
>>>  220:         return responses_factory.make_tool_call(build_mcp_function("ui", "end_turn"), {}, call_id="call_ui_end")
     221:
     222:     s = run_server(lambda model: make_mock(responses_create))
     223:     base = s["base_url"]
   ...
     272:     - Attempting to subscribe before MCP servers are attached is handled gracefully
     273:     - System queues or rejects the request appropriately
     274:     - No crashes or undefined behavior
>>>  275:     """
>>>  276:
>>>  277:     async def responses_create(_req):
>>>  278:         # Try to subscribe immediately (before server attached)
>>>  279:         return responses_factory.make_tool_call(
>>>  280:             build_mcp_function("resources", "subscribe"),
>>>  281:             {"server": "not_yet_attached", "uri": "resource://test/data"},
>>>  282:             call_id="call_early_sub",
>>>  283:         )
     284:
     285:     s = run_server(lambda model: make_mock(responses_create))
     286:     base = s["base_url"]
```

### `test-error-swallow.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/test-error-swallow.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/tests/agent/e2e/test_mcp_concurrent.py#L75-L82)

File: `adgn/tests/agent/e2e/test_mcp_concurrent.py` (L75-82)

> Tests use bare `except Exception:` blocks that swallow all errors, hiding real failures.
>
> Two pattern variations: `except Exception: break` in retry loops (lines 75-82 in
> test_mcp_concurrent.py) and `except Exception: pass` for optional operations (lines 171-175,
> 251-255 in test_mcp_edge_cases.py).
>
> This hides actual errors during test execution. If operations fail for real reasons (element
> not found, page crashed, network failure, timeout), the test silently continues and may pass
> when it should fail.
>
> Remove try/except entirely if operation should succeed, or catch only specific expected
> exceptions (TimeoutError, ElementNotFoundError). Let real errors propagate. If approvals are
> optional, check conditions explicitly rather than swallowing all errors.
>
> **Note:** Error-swallowing in approval loop with `except Exception: break`

```
      72:     wait_for_pending_approvals(page)
      73:
      74:     # Auto-approve all pending approvals by clicking approve repeatedly
>>>   75:     for _ in range(15):  # 5 agents x 3 calls each = 15 approvals
>>>   76:         try:
>>>   77:             approve_btn = page.get_by_role("button", name="Approve").first
>>>   78:             if approve_btn.count() > 0:
>>>   79:                 approve_btn.click()
>>>   80:                 page.wait_for_timeout(100)  # Small delay between approvals
>>>   81:         except Exception:
>>>   82:             break
      83:
      84:     # Verify all agents finished (check for finished status)
      85:     # The UI should show updates for all agents without missing any
```

### `test-error-swallow.yaml` / `occ-1`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/test-error-swallow.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/tests/agent/e2e/test_mcp_edge_cases.py#L171-L255)

File: `adgn/tests/agent/e2e/test_mcp_edge_cases.py` (L171-175, L251-255)

> Tests use bare `except Exception:` blocks that swallow all errors, hiding real failures.
>
> Two pattern variations: `except Exception: break` in retry loops (lines 75-82 in
> test_mcp_concurrent.py) and `except Exception: pass` for optional operations (lines 171-175,
> 251-255 in test_mcp_edge_cases.py).
>
> This hides actual errors during test execution. If operations fail for real reasons (element
> not found, page crashed, network failure, timeout), the test silently continues and may pass
> when it should fail.
>
> Remove try/except entirely if operation should succeed, or catch only specific expected
> exceptions (TimeoutError, ElementNotFoundError). Let real errors propagate. If approvals are
> optional, check conditions explicitly rather than swallowing all errors.
>
> **Note:** Error-swallowing in optional approval checks with `except Exception: pass`

```
     168:     send_prompt(page, "subscribe to resource")
     169:
     170:     # Wait for approval (if needed) and approve
>>>  171:     try:
>>>  172:         wait_for_pending_approvals(page, count=1, timeout=5000)
>>>  173:         approve_first_pending(page)
>>>  174:     except Exception:
>>>  175:         pass  # No approval needed
     176:
     177:     # Wait for run to finish
     178:     page.get_by_text("Status: finished").wait_for(timeout=10000)
   ...
     248:     send_prompt(page, "read slow resource")
     249:
     250:     # Wait for approval if needed and approve
>>>  251:     try:
>>>  252:         wait_for_pending_approvals(page, count=1, timeout=5000)
>>>  253:         approve_first_pending(page)
>>>  254:     except Exception:
>>>  255:         pass  # No approval needed
     256:
     257:     # Wait for run to complete (with extended timeout due to slow resource)
     258:     page.get_by_text("Status: finished").wait_for(timeout=15000)
```

### `test-overuse-suppress-exception.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/test-overuse-suppress-exception.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/tests/agent/e2e/test_mcp_errors.py#L94-L232)

File: `adgn/tests/agent/e2e/test_mcp_errors.py` (L94-96, L102-103, L147-148, L153-155, L230-232)

> Multiple uses of `with suppress(Exception):` to hide errors in tests that are meant
> to verify error handling behavior.
>
> **Current pattern (appears 5 times):**
>
> ```python
> with suppress(Exception):
>     # Server attachment might fail; we're testing error handling
>     requests.patch(base + f"/api/agents/{agent_id}/mcp", json={"attach": spec})
> ```
>
> **When an operation is expected to fail in a test:**
>
> - Use `pytest.raises(SpecificException)` to verify the specific error occurs
> - Assert on the error message or error state
> - Don't hide failures with blanket suppression
>
> **Correct approach:**
> Remove `suppress(Exception)` calls. Either:
>
> 1. Assert the operation succeeds (if it should)
> 2. Assert the operation fails with specific exception (using pytest.raises)
> 3. Verify the system handles the error appropriately (check error state, logs, etc.)
>
> Suppressing all exceptions makes the test unable to detect when something goes wrong.

```
      91:     spec = {"broken": {"transport": "inproc", "factory": "tests.agent.e2e.test_mcp_errors:_make_broken_server"}}
      92:     # Note: We use a factory string that will attempt to create a server with malformed responses
      93:     # This tests the error path when server produces invalid data
>>>   94:     with suppress(Exception):
>>>   95:         # Server attachment might fail; we're testing error handling
>>>   96:         requests.patch(base + f"/api/agents/{agent_id}/mcp", json={"attach": spec})
      97:
      98:     # Open UI
      99:     page.goto(base + f"/?agent_id={agent_id}")
     100:
     101:     # Wait for WS connection (may succeed even if MCP attachment failed)
>>>  102:     with suppress(Exception):
>>>  103:         page.locator(".ws .dot.on").wait_for(timeout=5000)
     104:
     105:     # Try to interact and verify UI shows error gracefully
     106:     send_prompt(page, "test broken resource")
   ...
     144:     # Note: For this test to work properly, we'd need the server to be properly instantiated
     145:     # For now, we test the UI's resilience to slow operations
     146:     spec = {"slow": {"transport": "inproc", "factory": "tests.agent.e2e.test_mcp_errors:_make_slow_server"}}
>>>  147:     with suppress(Exception):
>>>  148:         requests.patch(base + f"/api/agents/{agent_id}/mcp", json={"attach": spec})
     149:
     150:     # Open UI
     151:     page.goto(base + f"/?agent_id={agent_id}")
     152:
>>>  153:     with suppress(Exception):
>>>  154:         # Wait for connection with shorter timeout
>>>  155:         page.locator(".ws .dot.on").wait_for(timeout=5000)
     156:
     157:     # Verify UI is still responsive
     158:     assert page.title() is not None
   ...
     227:     assert page.title() is not None
     228:
     229:     # Check if WS connection is still active (should be, agent still exists)
>>>  230:     with suppress(Exception):
>>>  231:         # If connection indicator changed, that's expected behavior
>>>  232:         page.locator(".ws .dot.on").wait_for(timeout=2000)
     233:
     234:     s["stop"]()
     235:
```

### `untyped-tuple-returns.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/issues/untyped-tuple-returns.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-02/code/adgn/src/adgn/agent/persist/__init__.py#L188-L189)

File: `adgn/src/adgn/agent/persist/__init__.py` (L188, L189)

> Lines 188-189 define policy persistence methods with unclear return types:
> `get_latest_policy` returns `tuple[str, int] | None` where tuple unpacking
> requires remembering the order and the int's meaning (policy ID) is non-obvious.
> `set_policy` returns an undocumented int (the database-assigned policy ID).
>
> Problems: Tuple unpacking requires remembering element order, no semantic meaning
> to tuple positions, unclear what the int represents, requires checking None before
> unpacking, callers must know implementation details.
>
> Replace with a typed object (PolicyRecord or NamedTuple) containing id, content,
> timestamp, and agent_id fields. This provides self-documenting field names,
> type safety, IDE autocomplete, and clear semantics. Alternatively, at minimum
> add docstring documenting what the int represents.

```
     185:     async def load_events(self, run_id: UUID) -> list[EventRecord]: ...
     186:
     187:     # Approval policy (per-agent) --------------------------------------------
>>>  188:     async def get_latest_policy(self, agent_id: AgentID) -> tuple[str, int] | None: ...
>>>  189:     async def set_policy(self, agent_id: AgentID, *, content: str) -> int: ...
     190:
     191:     # Approval policy proposals (single store impl: SQLite)
     192:     async def create_policy_proposal(self, agent_id: AgentID, *, proposal_id: int, content: str) -> int: ...
```

## ducktape/2025-12-04-00 (21)

### `cast-may-be-unnecessary.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/cast-may-be-unnecessary.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/props/grader/models.py#L307)

File: `adgn/src/adgn/props/grader/models.py` (L307)

> Line 307 uses cast(GradeValidationContext, ctx) after already checking isinstance.
> After the isinstance check on line 305, mypy should already know the type.
> The cast may be unnecessary redundancy.

```
     304:         if ctx is None or not isinstance(ctx, GradeValidationContext):
     305:             return None
     306:         return cast(GradeValidationContext, ctx)
>>>  307:
     308:     @property
     309:     def _mentioned_tp_ids(self) -> set[InputIssueID]:
     310:         """Input IDs mentioned in canonical TP coverage."""
```

### `dead-code.yaml` / `occ-7`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/dead-code.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/openai_utils/model.py#L348-L357)

File: `adgn/src/adgn/openai_utils/model.py` (L348-357)

> Dead code that should be removed. These are definitions with zero call sites,
> commented-out code, or infrastructure left over from migrations.
>
> **Note:** Dead `responses` property providing unused .responses.create() interface

```
     345: class OpenAIModel:
     346:     client: AsyncOpenAI
     347:
>>>  348:     @property
>>>  349:     def responses(self):  # Pydantic-only surface: .responses.create(ResponsesRequest)
>>>  350:         outer = self
>>>  351:
>>>  352:         class _Compat:
>>>  353:             async def create(self, req: ResponsesRequest) -> ResponsesResult:
>>>  354:                 result = await outer.responses_create(req)
>>>  355:                 return cast(ResponsesResult, result)
>>>  356:
>>>  357:         return _Compat()
     358:
     359:     async def responses_create(self, req: ResponsesRequest) -> ResponsesResult:
     360:         """Create a Responses completion (non-streaming) and convert to our types."""
```

### `dead-constants-runs-context.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/dead-constants-runs-context.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/props/runs_context.py#L15-L19)

File: `adgn/src/adgn/props/runs_context.py` (L15-19)

> Lines 15-19 define five constants (RUN_TYPE_CRITIC, RUN_TYPE_GRADER, INPUT_JSON, OUTPUT_JSON, EVENTS_JSONL)
> that are
> never imported or used anywhere in the codebase. The module's stated purpose is to be "the single source of
> truth for
> all runs-related path construction" and its docstring explicitly says "No path tokens ('grader',
> 'output.json', etc.)
> should be hardcoded outside this module."
>
> However, these constants are being unused/ignored and the some strings are hardcoded elsewhere instead:
>
> - "events.jsonl" hardcoded in cluster_unknowns.py:111, cli_app/shared.py:60, cli_app/main.py:536,
>   lint_issue.py:
> - [430, 430]
>   Either these constants should be used to replace the hardcoded strings, or they should be deleted as dead
>   code. The
>   module's purpose is being violated by not using these centralized constants.

```
      12: from adgn.props.prop_utils import pkg_dir
      13:
      14: # Path token constants - single source of truth
>>>   15: RUN_TYPE_CRITIC = "critic"
>>>   16: RUN_TYPE_GRADER = "grader"
>>>   17: INPUT_JSON = "input.json"
>>>   18: OUTPUT_JSON = "output.json"
>>>   19: EVENTS_JSONL = "events.jsonl"
      20:
      21:
      22: def format_timestamp_session(dt: datetime | None = None) -> str:
```

### `duplicate-exit-code-constants.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/duplicate-exit-code-constants.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/mcp/_shared/constants.py#L18-L27)

File: `adgn/src/adgn/mcp/_shared/constants.py` (L18-27)

> Exit code constants (SIGNAL_EXIT_OFFSET, signal_exit_code(), EXIT_CODE_SIGTERM,
> EXIT_CODE_SIGKILL) are duplicated in both \_shared/constants.py and exec/models.py
> with identical definitions.
>
> This creates a maintenance burden and risks divergence. Since these constants are
> tightly coupled to the exec implementation and primarily used there, they should
> be defined in exec/models.py only.
>
> Resolution: Remove the duplicates from \_shared/constants.py and update
> container_session.py to import from exec/models.py instead.
>
> **Note:** First definition in shared constants

```
      15: RUNTIME_SERVER_NAME: Final[str] = "runtime"
      16: RUNTIME_EXEC_TOOL_NAME: Final[str] = "exec"
      17: RUNTIME_CONTAINER_INFO_URI: Final[str] = "resource://container.info"
>>>   18:
>>>   19: SIGNAL_EXIT_OFFSET: Final[int] = 128
>>>   20:
>>>   21:
>>>   22: def signal_exit_code(sig: int) -> int:
>>>   23:     return SIGNAL_EXIT_OFFSET + int(sig)
>>>   24:
>>>   25:
>>>   26: EXIT_CODE_SIGTERM: Final[int] = signal_exit_code(SIGTERM)
>>>   27: EXIT_CODE_SIGKILL: Final[int] = signal_exit_code(SIGKILL)
      28:
      29: # Common server names
      30: CRITIC_SUBMIT_SERVER_NAME: Final[str] = "critic_submit"
```

### `duplicate-exit-code-constants.yaml` / `occ-1`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/duplicate-exit-code-constants.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/mcp/exec/models.py#L14-L56)

File: `adgn/src/adgn/mcp/exec/models.py` (L14-19, L55-56)

> Exit code constants (SIGNAL_EXIT_OFFSET, signal_exit_code(), EXIT_CODE_SIGTERM,
> EXIT_CODE_SIGKILL) are duplicated in both \_shared/constants.py and exec/models.py
> with identical definitions.
>
> This creates a maintenance burden and risks divergence. Since these constants are
> tightly coupled to the exec implementation and primarily used there, they should
> be defined in exec/models.py only.
>
> Resolution: Remove the duplicates from \_shared/constants.py and update
> container_session.py to import from exec/models.py instead.
>
> **Note:** Duplicate definition in exec/models

```
      11:
      12: from pydantic import BaseModel, ConfigDict, Field
      13:
>>>   14: # Signal exit codes for process termination
>>>   15: SIGNAL_EXIT_OFFSET: Final[int] = 128
>>>   16:
>>>   17:
>>>   18: def signal_exit_code(sig: int) -> int:
>>>   19:     return SIGNAL_EXIT_OFFSET + int(sig)
      20:
      21:
      22: def perf_timer() -> float:
   ...
      52:     yield get_duration_ms
      53:
      54:
>>>   55: EXIT_CODE_SIGTERM: Final[int] = signal_exit_code(SIGTERM)
>>>   56: EXIT_CODE_SIGKILL: Final[int] = signal_exit_code(SIGKILL)
      57:
      58: # Cap for stdout/stderr/stdin bytes in exec-like servers
      59: MAX_BYTES_CAP = 100_000
```

### `manual-snapshot-yaml-update.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/manual-snapshot-yaml-update.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/props/cli_app/cmd_build_bundle.py#L335-L363)

File: `adgn/src/adgn/props/cli_app/cmd_build_bundle.py` (L335-363)

> The `cmd_build_bundle` function (lines 335-363) uses pygit2 to create filtered
> commits and tags for snapshot bundles, but doesn't return the mapping of tag names
> to commit SHAs that it creates. The function has no return value.
>
> This forces callers to either:
>
> 1. Write placeholder commit SHAs and manually update them later
> 2. Query the bundle post-hoc using `git bundle list-heads`
>
> The function calls `_build_bundle_internal` which creates the commits and tags.
> That commit information should be captured and returned to callers for automatic
> snapshots.yaml updates.

```
     332:         return p
     333:
     334:
>>>  335: def cmd_build_bundle(
>>>  336:     specimens_dir: Path | None = None, source_repo_path: Path | None = None, output_bundle: Path | None = None
>>>  337: ):
>>>  338:     """Build snapshot bundle with per-snapshot filters.
>>>  339:
>>>  340:     Args:
>>>  341:         specimens_dir: Base directory containing snapshots.yaml and snapshot subdirs (default: from package resources)
>>>  342:         source_repo_path: Path to source git repository (default: auto-discovered from current directory)
>>>  343:         output_bundle: Output path for bundle file (default: specimens_dir/ducktape/snapshots.bundle)
>>>  344:
>>>  345:     Note: The default output path matches the relative URL in snapshots.yaml (file://../snapshots.bundle
>>>  346:     resolved from specimens/ducktape/{snapshot}/ directories).
>>>  347:     """
>>>  348:     # Use defaults if not provided
>>>  349:     if specimens_dir is None:
>>>  350:         specimens_dir = get_specimens_dir()
>>>  351:     if source_repo_path is None:
>>>  352:         # Discover repository from current directory
>>>  353:         discovered = pygit2.discover_repository(".")
>>>  354:         if not discovered:
>>>  355:             raise RuntimeError("Could not find git repository. Run from within ducktape repo.")
>>>  356:         # pygit2.discover_repository returns path to .git directory, get parent
>>>  357:         source_repo_path = Path(discovered).parent if discovered.endswith("/.git/") else Path(discovered).parent.parent
>>>  358:     if output_bundle is None:
>>>  359:         # Default to specimens/ducktape/snapshots.bundle to match snapshots.yaml URLs
>>>  360:         output_bundle = specimens_dir / "ducktape" / "snapshots.bundle"
>>>  361:
>>>  362:     # Call internal implementation
>>>  363:     _build_bundle_internal(specimens_dir, source_repo_path, output_bundle)
```

### `missing-docker-memory-limit.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/missing-docker-memory-limit.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/mcp/_shared/container_session.py#L52-L129)

File: `adgn/src/adgn/mcp/_shared/container_session.py` (L99-129, L52-61)

> Docker containers created by ContainerOptions and \_build_host_config() do not
> specify memory limits. While containers are isolated by network mode ("none") and
> read-only volumes, it would be healthier to set explicit memory constraints.
>
> Setting memory limits helps:
>
> - Prevent runaway processes from affecting host stability
> - Make resource usage predictable and debuggable
> - Align with containerization best practices
>
> Docker supports Memory (hard limit) and MemoryReservation (soft limit) in HostConfig.
> A reasonable default could be 2-4GB for agent runtime containers and 1-2GB for
> critics/graders, with the option to override per use case.
>
> Example addition to \_build*host_config():
> host_config["Memory"] = opts.mem_limit or (2 * 1024 \_ 1024 \* 1024) # 2GB default

```
      49:     return shlex.join(list(cmd))
      50:
      51:
>>>   52: @dataclass
>>>   53: class ContainerOptions:
>>>   54:     image: str
>>>   55:     working_dir: Path = WORKING_DIR
>>>   56:     volumes: dict[str, dict[str, str]] | list[str] | None = None
>>>   57:     network_mode: str = "none"
>>>   58:     environment: dict[str, str] | None = None
>>>   59:     labels: dict[str, str] | None = None
>>>   60:     describe: bool = True
>>>   61:     ephemeral: bool = False
      62:
      63:     def to_container_config(
      64:         self,
   ...
      96:     return cast(ContainerSessionState, ctx.request_context.lifespan_context)
      97:
      98:
>>>   99: def _build_host_config(opts: ContainerOptions, *, auto_remove: bool = False) -> dict[str, Any]:
>>>  100:     """Build Docker HostConfig from ContainerOptions.
>>>  101:
>>>  102:     Args:
>>>  103:         opts: Container options with volumes and network_mode
>>>  104:         auto_remove: Whether to set AutoRemove (for per-session containers)
>>>  105:
>>>  106:     Returns:
>>>  107:         Docker HostConfig dict with Binds and NetworkMode if applicable
>>>  108:     """
>>>  109:     host_config: dict[str, Any] = {}
>>>  110:
>>>  111:     if auto_remove:
>>>  112:         host_config["AutoRemove"] = True
>>>  113:
>>>  114:     # Convert volumes to binds format
>>>  115:     if opts.volumes and isinstance(opts.volumes, dict):
>>>  116:         binds = []
>>>  117:         for host_path, volume_config in opts.volumes.items():
>>>  118:             bind = f"{host_path}:{volume_config['bind']}"
>>>  119:             if mode := volume_config.get("mode"):
>>>  120:                 bind += f":{mode}"
>>>  121:             binds.append(bind)
>>>  122:         if binds:
>>>  123:             host_config["Binds"] = binds
>>>  124:
>>>  125:     # Apply network mode if not 'none'
>>>  126:     if opts.network_mode != "none":
>>>  127:         host_config["NetworkMode"] = opts.network_mode
>>>  128:
>>>  129:     return host_config
     130:
     131:
     132: async def _start_container(*, client: aiodocker.Docker, opts: ContainerOptions) -> dict[str, Any]:
```

### `mkdir-in-wrong-location.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/mkdir-in-wrong-location.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/agent/transcript_handler.py#L36-L37)

File: `adgn/src/adgn/agent/transcript_handler.py` (L36-37)

> Lines 36-37 in transcript_handler.py create the parent directory in `__init__`, which performs I/O
> during object construction. The comment on line 36 ("Create parent directory if needed") and the mkdir
> operation should be moved to `_write_event()` where the file is actually written. This follows the
> principle of lazy initialization and reduces work done during object construction. The mkdir call can
> be performed once before the first write operation.

```
      33:
      34:     def __init__(self, *, events_path: Path) -> None:
      35:         self._events_path = events_path
>>>   36:         # Create parent directory if needed
>>>   37:         self._events_path.parent.mkdir(parents=True, exist_ok=True)
      38:         # Fail fast if a transcript already exists at destination
      39:         if self._events_path.exists():
      40:             raise FileExistsError(f"Transcript already exists: {self._events_path}")
```

### `nullable-with-defaults.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/nullable-with-defaults.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/props/cli_app/cmd_build_bundle.py#L18-L44)

File: `adgn/src/adgn/props/cli_app/cmd_build_bundle.py` (L18-44)

> The apply_gitignore_patterns function accepts include and exclude as list[str] | None,
> then checks "if include:" and "if exclude:" at lines 37-42. These parameters should
> instead be Sequence[str] with default=() in the function signature, eliminating the
> need for None checks. This makes the contract clearer and reduces defensive code.

```
      15: from adgn.props.models.snapshot import GitSource, SnapshotDoc
      16:
      17:
>>>   18: def apply_gitignore_patterns(file_list: list[str], include: list[str] | None, exclude: list[str] | None) -> list[str]:
>>>   19:     """Apply gitignore-style include/exclude patterns to a file list.
>>>   20:
>>>   21:     Include patterns are applied first (whitelist), then exclude patterns (blacklist).
>>>   22:     """
>>>   23:
>>>   24:     def matches_pattern(path: str, pattern: str) -> bool:
>>>   25:         """Check if path matches gitignore-style pattern."""
>>>   26:         # Remove trailing slash from pattern (indicates directory)
>>>   27:         if pattern.endswith("/"):
>>>   28:             pattern = pattern.rstrip("/")
>>>   29:             # For directory patterns, match the directory and everything under it
>>>   30:             return path.startswith(pattern + "/") or path == pattern
>>>   31:         # For file patterns, use fnmatch
>>>   32:         return fnmatch.fnmatch(path, pattern) or path.startswith(pattern + "/")
>>>   33:
>>>   34:     result = file_list
>>>   35:
>>>   36:     # Apply include patterns (if specified, only keep matching files)
>>>   37:     if include:
>>>   38:         result = [f for f in result if any(matches_pattern(f, pattern) for pattern in include)]
>>>   39:
>>>   40:     # Apply exclude patterns (remove matching files)
>>>   41:     if exclude:
>>>   42:         result = [f for f in result if not any(matches_pattern(f, pattern) for pattern in exclude)]
>>>   43:
>>>   44:     return result
      45:
      46:
      47: def get_tree_files(repo: pygit2.Repository, tree: pygit2.Tree, prefix: str = "") -> dict[str, tuple[pygit2.Oid, int]]:
```

### `openai-utils-bypass-reasoning-params.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/openai-utils-bypass-reasoning-params.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/openai_utils/model.py#L178-L391)

File: `adgn/src/adgn/openai_utils/model.py` (L390-391, L178)

> Lines 390-391 in `BoundOpenAIModel.responses_create()` manually construct the reasoning dict:
>
> ```python
> if self.reasoning_effort and "reasoning" not in kwargs:
>     kwargs["reasoning"] = {"effort": self.reasoning_effort.value}
> ```
>
> This bypasses the existing type-safe `ReasoningParams` TypedDict (defined in `openai_utils/types.py`) and
> duplicates
> conversion logic that already exists.
>
> The codebase already has:
>
> - `ReasoningParams` TypedDict with `effort` and `summary` fields (types.py)
> - `ResponsesRequest.reasoning: ReasoningParams | None` field (line 178)
> - `build_reasoning_params()` helper function for constructing ReasoningParams (types.py)
> - `to_kwargs()` which calls `model_dump()` and would automatically serialize ReasoningParams
>
> The manual dict construction is redundant and type-unsafe. Instead, `BoundOpenAIModel` should either:
>
> 1. Construct a proper `ReasoningParams` object and inject it into the request before calling `to_kwargs()`, OR
> 2. Let callers pass the reasoning params in the ResponsesRequest (which already has the field)
>
> Manual dict manipulation after `to_kwargs()` bypasses the type system and creates maintenance burden.

```
     175:     parallel_tool_calls: bool | None = None
     176:     stream: bool = False
     177:     store: bool | None = None
>>>  178:     reasoning: ReasoningParams | None = None
     179:     max_output_tokens: int | None = None
     180:
     181:     # Allow unknown fields for forward-compat (timeouts, metadata, etc.)
   ...
     387:         kwargs = req.to_kwargs()
     388:         # Enforce bound-model contract: always use the instance's model
     389:         kwargs["model"] = self.model
>>>  390:         if self.reasoning_effort and "reasoning" not in kwargs:
>>>  391:             kwargs["reasoning"] = {"effort": self.reasoning_effort.value}
     392:         sdk_resp: Response = await self.client.responses.create(**kwargs)
     393:         return ResponsesResult.from_sdk(sdk_resp)
     394:
```

### `openai-utils-redundant-singledispatch.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/openai-utils-redundant-singledispatch.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/openai_utils/model.py#L261-L273)

File: `adgn/src/adgn/openai_utils/model.py` (L261-273)

> Lines 261-273 contain three redundant `@singledispatch` registered functions that are identical - they all
> just return `item` unchanged with the same comment "No conversion needed, X is already an InputItem".
>
> While `@singledispatch.register` doesn't support Union types, you CAN register the same function for
> multiple types to avoid duplication:
>
> ```python
> def _identity(item: InputItem) -> InputItem:
>     return item
>
> response_out_item_to_input.register(ReasoningItem)(_identity)
> response_out_item_to_input.register(FunctionCallItem)(_identity)
> response_out_item_to_input.register(FunctionCallOutputItem)(_identity)
> ```
>
> This eliminates the redundant function definitions while maintaining the same dispatch behavior.

```
     258:     raise TypeError(f"Unsupported response item type: {type(item)!r}")
     259:
     260:
>>>  261: @response_out_item_to_input.register
>>>  262: def _(item: ReasoningItem) -> InputItem:
>>>  263:     return item  # No conversion needed, ReasoningItem is already an InputItem
>>>  264:
>>>  265:
>>>  266: @response_out_item_to_input.register
>>>  267: def _(item: FunctionCallItem) -> InputItem:
>>>  268:     return item  # No conversion needed, FunctionCallItem is already an InputItem
>>>  269:
>>>  270:
>>>  271: @response_out_item_to_input.register
>>>  272: def _(item: FunctionCallOutputItem) -> InputItem:
>>>  273:     return item  # No conversion needed, FunctionCallOutputItem is already an InputItem
     274:
     275:
     276: @response_out_item_to_input.register
```

### `redundant-checks.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/redundant-checks.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/props/cli_app/cmd_build_bundle.py#L224-L235)

File: `adgn/src/adgn/props/cli_app/cmd_build_bundle.py` (L224-235)

> Redundant checks and guards that serve no purpose and can be removed. These include checking the same
> condition twice,
> redundant None checks with isinstance, and redundant type validation.
>
> **Note:** Checks for bundle metadata twice - first at line 226 with dict.get(), then at line 233 with validated model

```
     221:         snapshots_data = yaml.safe_load(f) or {}
     222:
     223:     results = []
>>>  224:     for slug, snapshot_data in snapshots_data.items():
>>>  225:         # Skip snapshots without bundle metadata
>>>  226:         if not snapshot_data.get("bundle"):
>>>  227:             continue
>>>  228:
>>>  229:         # Parse and validate the snapshot doc (let validation errors propagate)
>>>  230:         snapshot = TypeAdapter(SnapshotDoc).validate_python(snapshot_data)
>>>  231:
>>>  232:         # Only include snapshots with complete bundle metadata
>>>  233:         if snapshot.bundle is not None:
>>>  234:             results.append((slug, snapshot))
>>>  235:
     236:     return results
     237:
     238:
```

### `redundant-checks.yaml` / `occ-2`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/redundant-checks.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/agent/agent.py#L87)

File: `adgn/src/adgn/agent/agent.py` (L87)

> Redundant checks and guards that serve no purpose and can be removed. These include checking the same
> condition twice,
> redundant None checks with isinstance, and redundant type validation.
>
> **Note:** Redundant isinstance check: "if not isinstance(call_id, str) or not call_id" - second condition is sufficient

```
      84:
      85: def _require_call_id(function_call: FunctionCallItem) -> str:
      86:     call_id = function_call.call_id
>>>   87:     if not isinstance(call_id, str) or not call_id:
      88:         raise RuntimeError("FunctionCallItem missing call_id")
      89:     return call_id
      90:
```

### `redundant-compositor-names.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/redundant-compositor-names.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/tests/conftest.py#L81-L473)

File: `adgn/tests/conftest.py` (L81, L355, L379, L411, L473)

> Multiple places instantiate `Compositor` with explicit name arguments (e.g., `Compositor("test")`,
> `Compositor("comp")`), but these names serve no functional purpose in most cases.
>
> The `Compositor` class has a default name of `"compositor"` (server.py:134), so passing a name explicitly is
> redundant
> unless:
>
> 1. The compositor is being mounted inside another compositor (two-level pattern)
> 2. There's a specific need to distinguish compositors in logs/debugging
>
> **Why this is a problem:**
>
> - The explicit names don't affect behavior or functionality
> - They add visual noise and unnecessary parameters
> - They create inconsistency (different tests use different arbitrary names: "test", "comp", "compositor")
> - In test fixtures, the name is completely unused since compositors are not nested
>
> **Exception: Two-level compositor pattern**
> The `compositor_factory.py` case is special - it creates a "global" compositor that mounts an agents server.
> If this
> compositor itself can be mounted in another compositor, the name might be meaningful for debugging nested
> compositor
> structures. However, even there, the default name would likely suffice.
>
> **Fix:**
> Remove the explicit name argument and rely on the default: `Compositor()` instead of `Compositor("name")`.
>
> **Note:** Test fixtures using arbitrary "comp" name - name never referenced

```
      78:
      79:
      80: def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
>>>   81:     for item in items:
      82:         if item.get_closest_marker("requires_sandbox_exec") is not None:
      83:             item.add_marker(pytest.mark.macos)
      84:
   ...
     352: @pytest.fixture
     353: async def pg_compositor_echo(echo_spec, make_pg_compositor):
     354:     """Async fixture with echo server and policy gateway.
>>>  355:
     356:     Yields (client, compositor, policy_engine).
     357:     """
     358:     async with make_pg_compositor(echo_spec) as result:
   ...
     376:     Yields (client, compositor, buffer) so tests can read buffered notifications
     377:     or pass buffer.poll into handlers.
     378:     """
>>>  379:
     380:     @asynccontextmanager
     381:     async def _open(servers: McpServerSpecs):
     382:         comp = Compositor("comp")
   ...
     408:     """Provide a live AsyncOpenAI client for tests marked with `live_llm`.
     409:
     410:     - For non-`live_llm` tests that include this fixture in the signature but
>>>  411:       do not actually use it (e.g., parameterized tests with a mock branch),
     412:       return a lightweight no-op placeholder to avoid network work and keep
     413:       those tests running.
     414:     - For `live_llm` tests, require OPENAI_API_KEY and construct AsyncOpenAI;
   ...
     470:     def _make(decision: ApprovalDecision) -> PolicyEngine:
     471:         policy_source = make_policy_source(decision)
     472:         return make_approval_policy_server(policy_source)
>>>  473:
     474:     return _make
     475:
     476:
```

### `redundant-path-construction.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/redundant-path-construction.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/inop/engine/models.py#L380)

File: `adgn/src/adgn/inop/engine/models.py` (L380)

> Line 380 converts self.workspace_path (str) to Path, but this field should already be typed as Path at the
> class
> definition level. The conversion is redundant if the model properly validates the field type on construction.

```
     377:             Dictionary mapping relative file paths to contents
     378:         """
     379:         files: dict[str, str] = {}
>>>  380:         directory_path = Path(self.workspace_path)
     381:
     382:         if not directory_path.exists():
     383:             return files
```

### `redundant-snapshot-hydration.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/redundant-snapshot-hydration.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/props/gepa/gepa_adapter.py#L195-L334)

File: `adgn/src/adgn/props/gepa/gepa_adapter.py` (L308-334, L195, L256)

> GEPA optimization repeatedly hydrates the same snapshots, causing ~200 redundant tar extractions and file
> discoveries
> during a typical optimization run.
>
> **The inefficiency:**
>
> Dataset loading (`load_datasets()`, lines 308-334) hydrates each snapshot once to extract metadata, then
> closes the
> hydrated context:
>
> ```python
> async with registry.load_and_hydrate(slug) as hydrated:
>     return SnapshotInput(slug=slug, target_files=..., ...)
> # Hydrated snapshot deleted here when context exits
> ```
>
> During optimization, each evaluation re-hydrates from scratch (`_evaluate_one_specimen()`, line 195):
>
> ```python
> async def _evaluate_one_specimen(self, specimen_input: SnapshotInput, ...):
>     async with self.registry.load_and_hydrate(slug) as hydrated:
>         # Run critic with fresh hydration
> ```
>
> **Performance impact:**
>
> With 5-10 unique snapshots and max_metric_calls=200:
>
> - Initial loading: 5-10 hydrations (~5-10 seconds)
> - Optimization evaluations: ~200 hydrations (~200-400 seconds total)
> - Each hydration: tar extraction, JSON parsing, file discovery (~1-2 seconds)
> - Same snapshot hydrated 20-40 times throughout the run
>
> **Why this matters:**
>
> Snapshots are mounted read-only to Docker containers, so the hydrated directories could be reused safely. The
> issue is
> architectural:
>
> - `SnapshotInput` stores only metadata (slug, target_files list, ground truth issues)
> - `HydratedSnapshot` objects are created and destroyed per-evaluation
> - No mechanism to keep snapshots hydrated throughout the GEPA run
>
> **Potential fix:**
>
> Keep `HydratedSnapshot` objects alive throughout GEPA optimization:
>
> - Load and hydrate snapshots once at start
> - Pass `HydratedSnapshot` references through the evaluation pipeline (not just metadata)
> - Reuse the same hydrated directories for all critic/grader runs
> - Clean up only at the end of GEPA run
>
> This would reduce ~200 hydrations to ~10, saving 3-6 minutes per optimization run.

```
     192:         slug = specimen_input.slug
     193:
     194:         # Run critic
>>>  195:         async with self.registry.load_and_hydrate(slug) as hydrated:
     196:             critic_input = CriticInput(snapshot_slug=slug, files=ALL_FILES_WITH_ISSUES, prompt_sha256=prompt_sha256)
     197:
     198:             critic_output, critic_run_id, critique_id = await run_critic(
   ...
     253:         prompt_sha256 = hash_and_upsert_prompt(system_prompt)
     254:
     255:         # Run all specimens in parallel
>>>  256:         tasks = [self._evaluate_one_specimen(specimen_input, prompt_sha256, capture_traces) for specimen_input in batch]
     257:         results = await asyncio.gather(*tasks)
     258:
     259:         return list(results)
   ...
     305: # =============================================================================
     306:
     307:
>>>  308: async def load_datasets(registry: SnapshotRegistry) -> tuple[list[SnapshotInput], list[SnapshotInput]]:
>>>  309:     """Load train and validation datasets for GEPA.
>>>  310:
>>>  311:     This function hydrates snapshots to discover target files and uses the registry's
>>>  312:     TruePositiveIssue and KnownFalsePositive formats which are compatible with the grader.
>>>  313:
>>>  314:     For source-of-truth data models, see TrainingExample and FilesystemLoader.
>>>  315:
>>>  316:     Returns:
>>>  317:         (trainset, valset) tuple of SnapshotInput lists
>>>  318:     """
>>>  319:     train_slugs = registry.get_snapshots_by_split(Split.TRAIN)
>>>  320:     valid_slugs = registry.get_snapshots_by_split(Split.VALID)
>>>  321:
>>>  322:     async def load_snapshot(slug: SnapshotSlug) -> SnapshotInput:
>>>  323:         async with registry.load_and_hydrate(slug) as hydrated:
>>>  324:             return SnapshotInput(
>>>  325:                 slug=slug,
>>>  326:                 target_files=hydrated.files_with_issues(),
>>>  327:                 known_true_positives=hydrated.true_positives,
>>>  328:                 known_false_positives=hydrated.false_positives,
>>>  329:             )
>>>  330:
>>>  331:     trainset = [await load_snapshot(slug) for slug in train_slugs]
>>>  332:     valset = [await load_snapshot(slug) for slug in valid_slugs]
>>>  333:
>>>  334:     return trainset, valset
     335:
     336:
     337: def load_training_examples(specimens_dir: Path | None = None) -> tuple[list[TrainingExample], list[TrainingExample]]:
```

### `session-passing.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/session-passing.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/props/cli_app/cmd_db.py#L47-L63)

File: `adgn/src/adgn/props/cli_app/cmd_db.py` (L47-63)

> Inconsistent session management: sync_detector_prompts() and sync_model_metadata()
> don't take a session parameter, while sync_snapshots_to_db() and sync_issues_to_db() do.
> This forces sync_all() to open a session for only 2 of 4 operations, then call the
> other 2 outside the session context.
>
> All four sync functions should take a session parameter for consistency, allowing
> sync_all() to be written as a single with-block that inlines the FullSyncResult
> construction with all four calls inside the session context.

```
      44:     ]
      45:
      46:
>>>   47: def sync_all() -> FullSyncResult:
>>>   48:     """Sync snapshots, issues, detector prompts, and model metadata in a single operation.
>>>   49:
>>>   50:     Returns:
>>>   51:         Combined results from all sync operations
>>>   52:     """
>>>   53:     registry = SnapshotRegistry.from_package_resources()
>>>   54:     with get_session() as session:
>>>   55:         snapshot_stats = sync_snapshots_to_db(session, registry)
>>>   56:         issue_stats = sync_issues_to_db(session, registry)
>>>   57:
>>>   58:     return FullSyncResult(
>>>   59:         snapshot_stats=snapshot_stats,
>>>   60:         issue_stats=issue_stats,
>>>   61:         detector_prompts=sync_detector_prompts(),
>>>   62:         model_metadata_stats=sync_model_metadata(),
>>>   63:     )
      64:
      65:
      66: def recreate_database_schema() -> tuple[SyncStats, SyncStats]:
```

### `string-replace-db-url.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/string-replace-db-url.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/props/prompt_optimizer.py#L370-L379)

File: `adgn/src/adgn/props/prompt_optimizer.py` (L370-379)

> Lines 370-379 read the database URL from environment variable PROPS_AGENT_DB_URL, then use string replacement
> `agent_db_url.replace("localhost:5433", "props-postgres:5432")` to transform the host-side URL into a
> container-accessible URL for Docker network access. This string manipulation is fragile and error-prone - it
> assumes a
> specific URL format and hardcodes both the source and target host/port values.

```
     367:         # No longer mount libsonnet definitions from filesystem
     368:
     369:         # Get agent_user database URL from environment
>>>  370:         agent_db_url = os.environ.get("PROPS_AGENT_DB_URL")
>>>  371:         logger.info(f"PROPS_AGENT_DB_URL from environment: {agent_db_url}")
>>>  372:         if not agent_db_url:
>>>  373:             logger.warning(
>>>  374:                 "PROPS_AGENT_DB_URL not set - agent will not have database access. "
>>>  375:                 "Set to enable querying train data and valid aggregates."
>>>  376:             )
>>>  377:         else:
>>>  378:             # Transform localhost:5433 → props-postgres:5432 for Docker network access
>>>  379:             agent_db_url = agent_db_url.replace("localhost:5433", "props-postgres:5432")
     380:             logger.info(f"Transformed agent_db_url for container: {agent_db_url}")
     381:
     382:         # Create Docker wiring (no /repo mount - would leak test specimen definitions!)
```

### `subscribe-tools-wrong-input-type.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/subscribe-tools-wrong-input-type.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/mcp/resources/server.py#L51-L413)

File: `adgn/src/adgn/mcp/resources/server.py` (L386, L413, L51-52)

> Lines 386 and 413 define subscribe/unsubscribe tools that use ResourcesReadArgs as their input type. However,
> ResourcesReadArgs includes windowing parameters (start_offset and max_bytes on lines 51-52) that are only
> relevant for
> reading resources, not for subscribing/unsubscribing.
>
> The subscribe and unsubscribe tools only use input.server and input.uri - they never access the windowing
> parameters.
> These tools should use a separate, simpler input type (e.g., ResourceSubscriptionArgs) with just server and
> uri
> fields,
> making the tool interface clearer and avoiding unnecessary parameters.

```
      48: class ResourcesReadArgs(BaseModel):
      49:     server: str = Field(description="Origin MCP server name that owns the resource")
      50:     uri: str = Field(description="Resource URI as reported by the origin server's list")
>>>   51:     start_offset: int = Field(default=0, ge=0, description="Start byte offset for windowed reads")
>>>   52:     max_bytes: int = Field(default=0, ge=0, description="Max bytes to return (0 means no limit)")
      53:     model_config = ConfigDict(extra="forbid")
      54:
      55:
   ...
     383:         return _build_window_payload(contents, input.start_offset, None if input.max_bytes == 0 else input.max_bytes)
     384:
     385:     @mcp.flat_model()
>>>  386:     async def subscribe(input: ResourcesReadArgs) -> SimpleOk:
     387:         """Subscribe to updates for a resource."""
     388:         await _ensure_capability(input.server, feature=ResourceCapabilityFeature.SUBSCRIBE)
     389:         prefixed = add_resource_prefix(input.uri, input.server, compositor.resource_prefix_format)
   ...
     410:             return SimpleOk(ok=True)
     411:
     412:     @mcp.flat_model()
>>>  413:     async def unsubscribe(input: ResourcesReadArgs) -> SimpleOk:
     414:         """Unsubscribe from updates for a resource."""
     415:         await _ensure_capability(input.server, feature=ResourceCapabilityFeature.SUBSCRIBE)
     416:         prefixed = add_resource_prefix(input.uri, input.server, compositor.resource_prefix_format)
```

### `unnecessary-tuple-unpacking.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/unnecessary-tuple-unpacking.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/src/adgn/mcp/approval_policy/engine.py#L566-L568)

File: `adgn/src/adgn/mcp/approval_policy/engine.py` (L566-568)

> The active_policy() resource handler unnecessarily calls get_policy() which
> returns a tuple (source, version), then unpacks and discards the version:
>
> Current code (lines 566-568):
> def active_policy() -> str:
> content, \_version = self.get_policy()
> return content
>
> This is awkward and requires unpacking a tuple just to discard half of it.
> Since the function only needs the policy source, it should directly access
> the private field:
>
> def active_policy() -> str:
> return self.\_policy_source
>
> Note: get_policy() has legitimate users that need both source and version
> (tests in test_preset_policy_loading.py verify version increments). But this
> resource handler only needs the source.
>
> Similar pattern exists in agent/policy_eval/container.py:38, but that's in
> a different context (agent layer calling into MCP layer).

```
     563:             if (got := await self.persistence.get_policy_proposal(self.agent_id, id)) is None:
     564:                 raise KeyError(id)
     565:             return got.content
>>>  566:
>>>  567:         @self.reader.resource(PENDING_CALLS_URI, name="pending_calls", mime_type="application/json")
>>>  568:         def pending_calls() -> dict:
     569:             """List all pending tool call approval requests."""
     570:             items = [
     571:                 PendingCallItem(
```

### `unused-seeded-prompts.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/issues/unused-seeded-prompts.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-12-04-00/code/adgn/tests/props/conftest.py#L255-L260)

File: `adgn/tests/props/conftest.py` (L255-260)

> The test_db fixture seeds four Prompt records that are never used by any test.
> Lines 257-260 create prompts with sha256 values "test123", "unknown", "test", and
> "train-test", but no test queries or references these values. All tests that use
> the Prompt table either create their own prompts (e.g., test_agent_queries.py
> line 105 creates "a"\*64) or call load_and_upsert_detector_prompt() which creates
> its own entries. These seeded prompts should be deleted.

```
     252:     init_db(test_config.admin_url)
     253:     recreate_database()
     254:
>>>  255:     # Create default test prompts
>>>  256:     with get_session() as session:
>>>  257:         for prompt_sha256 in ["test123", "unknown", "test", "train-test"]:
>>>  258:             prompt = Prompt(prompt_sha256=prompt_sha256, prompt_text=f"Test prompt for {prompt_sha256}")
>>>  259:             session.add(prompt)
>>>  260:         session.commit()
     261:
     262:     yield  # Test runs here
     263:
```

## ducktape_llm_common/2026-01-03-00 (20)

### `dict-instead-of-pydantic-model.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/dict-instead-of-pydantic-model.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/tests/claude_linter_v2/test_diff_intelligence.py#L49)

File: `tests/claude_linter_v2/test_diff_intelligence.py` (L49)

> Tests construct `edits` as a list of raw dicts `[{"old_string": ..., "new_string": ...}]`
> when the typed `EditOperation` Pydantic model exists (claude_linter/models.py:45-58).
>
> Per STYLE.md: "Instantiate Pydantic models with explicit keyword arguments rather than
> passing raw dicts." While Pydantic coerces dicts, explicit construction provides type
> safety and makes the expected type clear at the call site.
>
> Use `[EditOperation(old_string="foo", new_string="bar")]` instead.
>
> **Note:** edits=[{...}] should use EditOperation models

```
      46:             tool_name="MultiEdit",
      47:             tool_input={
      48:                 "file_path": "/test.py",
>>>   49:                 "edits": [{"old_string": "foo", "new_string": "bar"}, {"old_string": "baz", "new_string": "qux"}],
      50:             },
      51:             tool_response={
      52:                 "structuredPatch": [
```

### `integration-tests-not-isolated.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/integration-tests-not-isolated.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/tests/claude_linter_v2/test_integration.py#L27)

File: `tests/claude_linter_v2/test_integration.py` (L27)

> Integration tests use hardcoded session IDs and write to the real user data
> directory (~/.local/share/claude-linter-v2/sessions/). This causes:
>
> 1. Tests to interfere with each other across runs
> 2. Tests to pick up stale state from previous runs
> 3. Potential interference with real user session data
> 4. Non-deterministic test failures when stale data has incompatible format
>
> **Note:** Uses fixed session_id '12345678-1234-5678-1234-567812345678' that persists to real ~/.local/share path

```
      24:     pass
      25: """,
      26:             },
>>>   27:             "session_id": "12345678-1234-5678-1234-567812345678",
      28:         }
      29:
      30:         result = subprocess.run(
```

### `path-prefix-match.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/path-prefix-match.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/ducktape_llm_common/claude_linter_v2/session/manager.py#L129-L150)

File: `ducktape_llm_common/claude_linter_v2/session/manager.py` (L129, L148-150)

> `add_rule` uses `str.startswith()` (line 149) to test whether a session's
> directory is under the target directory. String prefix matching is not
> equivalent to path containment: a target of `/home/user/proj` incorrectly
> matches `/home/user/project-old` because the string starts with the same
> prefix. The rule is then applied to sessions belonging to unrelated
> directories. Should use `Path.is_relative_to()` or compare with a trailing
> separator.

```
     126:     ) -> int:
     127:         """Add a permission rule to session(s)."""
     128:         directory = directory or Path.cwd()
>>>  129:         directory_str = str(directory.resolve())
     130:
     131:         rule = Rule(predicate=predicate, action=action, created=datetime.now(), expires=expires)
     132:
   ...
     145:                 session_data = self._load_session(sid)
     146:
     147:                 # Skip if session is in different directory
>>>  148:                 session_dir = str(session_data.directory) if session_data.directory else ""
>>>  149:                 if not session_dir.startswith(directory_str):
>>>  150:                     continue
     151:
     152:                 # Add rule to this session
     153:                 session_data.rules.append(rule.model_copy())
```

### `redundant-inner-error-handling.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/redundant-inner-error-handling.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/ducktape_llm_common/claude_hook.py#L125-L128)

File: `ducktape_llm_common/claude_hook.py` (L125-128)

> Inner try/except for request parsing is redundant when an outer error boundary
> already catches all exceptions. The pattern duplicates error handling and
> clutters the code. Let the outer boundary handle it.
>
> **Note:** Inner try/except duplicates outer boundary at line 170

```
     122:                 outcome = HookError(f"Invalid request format: {e!s}")
     123:                 response = outcome.to_claude_response()
     124:                 print(response.model_dump_json(by_alias=True))
>>>  125:                 sys.exit(0)
>>>  126:
>>>  127:             # Generate invocation ID and set up logging
>>>  128:             invocation_id = InvocationID(uuid.uuid4())
     129:
     130:             # Set up logger for this session/invocation
     131:             logger = get_session_logger(hook_instance.hook_name, request.session_id, invocation_id)
```

### `request-session-id-typed-as-str.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/request-session-id-typed-as-str.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/tests/claude_linter_v2/test_mcp_tools.py#L34-L269)

File: `tests/claude_linter_v2/test_mcp_tools.py` (L34, L55, L82, L95, L114, L143, L167, L180, L203, L235, L248, L269)

> Hook request models (PreToolUseRequest, PostToolUseRequest, StopRequest, etc.)
> have session_id typed as str, forcing callers to write `session_id=str(session_id)`.
>
> The models should accept SessionID directly. Pydantic handles UUID serialization
> natively - no custom serializer needed. This eliminates repetitive str() conversions
> and provides type safety at the API boundary.
>
> **Note:** session_id=str(session_id) should be session_id=session_id

```
      31:         """Test that MCP tools with custom fields are handled correctly."""
      32:         # Create a request with MCP-specific fields
      33:         request = PreToolUseRequest(
>>>   34:             session_id=str(session_id),
      35:             hook_event_name="PreToolUse",
      36:             tool_name="mcp_memory_search_nodes",
      37:             tool_input=ToolInput(
   ...
      52:     def test_mcp_puppeteer_tool(self, handler, session_id):
      53:         """Test MCP puppeteer tool with its specific parameters."""
      54:         request = PostToolUseRequest(
>>>   55:             session_id=str(session_id),
      56:             hook_event_name="PostToolUse",
      57:             tool_name="mcp_puppeteer_navigate",
      58:             tool_input=ToolInput(url="https://example.com", allowDangerous=True, wait_for="networkidle2"),
   ...
      79:         type(handler.config_loader).config = mock_config
      80:
      81:         request = PreToolUseRequest(
>>>   82:             session_id=str(session_id),
      83:             hook_event_name="PreToolUse",
      84:             tool_name="mcp_filesystem_write_file",
      85:             tool_input=ToolInput(path="/tmp/test.txt", content="Hello, world!"),
   ...
      92:     def test_unknown_mcp_tool_fields(self, handler, session_id):
      93:         """Test that unknown MCP tool fields don't cause errors."""
      94:         request = PreToolUseRequest(
>>>   95:             session_id=str(session_id),
      96:             hook_event_name="PreToolUse",
      97:             tool_name="mcp_custom_tool",
      98:             tool_input=ToolInput(
   ...
     111:     def test_mcp_tool_with_python_file(self, handler, session_id):
     112:         """Test MCP tool operating on Python files."""
     113:         request = PreToolUseRequest(
>>>  114:             session_id=str(session_id),
     115:             hook_event_name="PreToolUse",
     116:             tool_name="mcp_editor_open",
     117:             tool_input=ToolInput(
   ...
     140:         test_file.write_text("import os\nimport sys\n\n\ndef test():\n    pass")
     141:
     142:         request = PostToolUseRequest(
>>>  143:             session_id=str(session_id),
     144:             hook_event_name="PostToolUse",
     145:             tool_name="mcp_editor_save",
     146:             tool_input=ToolInput(file_path=str(test_file), content="import os\nimport sys\n\n\ndef test():\n    pass"),
   ...
     164:
     165:         for tool_name in tool_names:
     166:             request = PreToolUseRequest(
>>>  167:                 session_id=str(session_id),
     168:                 hook_event_name="PreToolUse",
     169:                 tool_name=tool_name,
     170:                 tool_input=ToolInput(query="test"),
   ...
     177:     def test_mcp_tool_with_complex_result(self, handler, session_id):
     178:         """Test MCP tool with complex nested result structure."""
     179:         request = PostToolUseRequest(
>>>  180:             session_id=str(session_id),
     181:             hook_event_name="PostToolUse",
     182:             tool_name="mcp_knowledge_graph_query",
     183:             tool_input=ToolInput(query="MATCH (n:Node) RETURN n LIMIT 10", database="neo4j"),
   ...
     200:     def test_mcp_tool_error_result(self, handler, session_id):
     201:         """Test MCP tool that returned an error."""
     202:         request = PostToolUseRequest(
>>>  203:             session_id=str(session_id),
     204:             hook_event_name="PostToolUse",
     205:             tool_name="mcp_api_call",
     206:             tool_input=ToolInput(endpoint="/api/users", method="GET"),
   ...
     232:         tool_input_dict = {"file_path": None, "content": None, **input_fields}
     233:
     234:         request = PreToolUseRequest(
>>>  235:             session_id=str(session_id),
     236:             hook_event_name="PreToolUse",
     237:             tool_name=tool_name,
     238:             tool_input=ToolInput(**tool_input_dict),
   ...
     245:     def test_mcp_tool_session_tracking(self, handler, session_id):
     246:         """Test that MCP tools properly track sessions."""
     247:         request = PreToolUseRequest(
>>>  248:             session_id=str(session_id),
     249:             hook_event_name="PreToolUse",
     250:             tool_name="mcp_workspace_list",
     251:             tool_input=ToolInput(directory="/home/user/project"),
   ...
     266:     def test_mcp_tool_with_file_path_updates_working_dir(self, handler, session_id):
     267:         """Test that MCP tools with file paths update the working directory."""
     268:         request = PreToolUseRequest(
>>>  269:             session_id=str(session_id),
     270:             hook_event_name="PreToolUse",
     271:             tool_name="mcp_file_manager_open",
     272:             tool_input=ToolInput(file_path="/home/user/projects/myapp/src/main.py", content="# Main file"),
```

### `request-session-id-typed-as-str.yaml` / `occ-1`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/request-session-id-typed-as-str.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/tests/claude_linter_v2/test_stop_hook_quality_gate.py#L54-L166)

File: `tests/claude_linter_v2/test_stop_hook_quality_gate.py` (L54, L93, L107, L133, L166)

> Hook request models (PreToolUseRequest, PostToolUseRequest, StopRequest, etc.)
> have session_id typed as str, forcing callers to write `session_id=str(session_id)`.
>
> The models should accept SessionID directly. Pydantic handles UUID serialization
> natively - no custom serializer needed. This eliminates repetitive str() conversions
> and provides type safety at the API boundary.
>
> **Note:** StopRequest session_id should accept SessionID directly

```
      51:     )
      52:
      53:     # Create stop hook request
>>>   54:     request = StopRequest(hook_event_name="Stop", session_id=str(session_id))
      55:
      56:     # Handle the hook
      57:     result = handler.handle("Stop", request)
   ...
      90:     )
      91:
      92:     # Create stop hook request
>>>   93:     request = StopRequest(hook_event_name="Stop", session_id=str(session_id))
      94:
      95:     # Handle the hook
      96:     result = handler.handle("Stop", request)
   ...
     104: def test_stop_hook_passes_with_no_violations(handler, session_id, tmp_path):
     105:     """Test that stop hook passes when there are no violations."""
     106:     # Create stop hook request
>>>  107:     request = StopRequest(hook_event_name="Stop", session_id=str(session_id))
     108:
     109:     # Handle the hook
     110:     result = handler.handle("Stop", request)
   ...
     130:     )
     131:
     132:     # Create stop hook request
>>>  133:     request = StopRequest(hook_event_name="Stop", session_id=str(session_id))
     134:
     135:     # Handle the hook
     136:     result = handler.handle("Stop", request)
   ...
     163:     assert unfixed[0].file_path == "/test/other.py"
     164:
     165:     # Stop hook should only report the unfixed violation
>>>  166:     request = StopRequest(hook_event_name="Stop", session_id=str(session_id))
     167:
     168:     result = handler.handle("Stop", request)
     169:     # Since only warnings remain, should allow stop
```

### `request-session-id-typed-as-str.yaml` / `occ-2`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/request-session-id-typed-as-str.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/tests/claude_linter_v2/test_stop_hook_fresh_scan.py#L45-L140)

File: `tests/claude_linter_v2/test_stop_hook_fresh_scan.py` (L45, L84, L111, L140)

> Hook request models (PreToolUseRequest, PostToolUseRequest, StopRequest, etc.)
> have session_id typed as str, forcing callers to write `session_id=str(session_id)`.
>
> The models should accept SessionID directly. Pydantic handles UUID serialization
> natively - no custom serializer needed. This eliminates repetitive str() conversions
> and provides type safety at the API boundary.
>
> **Note:** StopRequest session_id should accept SessionID directly

```
      42: """)
      43:
      44:     # Create stop hook request
>>>   45:     request = StopRequest(hook_event_name="Stop", session_id=str(session_id))
      46:
      47:     # Handle the hook
      48:     result = handler.handle("Stop", request)
   ...
      81: """)
      82:
      83:     # Create stop hook request
>>>   84:     request = StopRequest(hook_event_name="Stop", session_id=str(session_id))
      85:
      86:     # Handle the hook
      87:     result = handler.handle("Stop", request)
   ...
     108:     (tmp_path / "README.md").write_text("# except hasattr")
     109:
     110:     # Create stop hook request
>>>  111:     request = StopRequest(hook_event_name="Stop", session_id=str(session_id))
     112:
     113:     # Handle the hook
     114:     result = handler.handle("Stop", request)
   ...
     137:     bad_file.write_text("except: pass")  # Invalid syntax but we don't care
     138:
     139:     # Create stop hook request
>>>  140:     request = StopRequest(hook_event_name="Stop", session_id=str(session_id))
     141:
     142:     # Handle the hook
     143:     result = handler.handle("Stop", request)
```

### `request-session-id-typed-as-str.yaml` / `occ-3`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/request-session-id-typed-as-str.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/tests/claude_linter_v2/test_stop_hook_gitignore.py#L90-L143)

File: `tests/claude_linter_v2/test_stop_hook_gitignore.py` (L90, L143)

> Hook request models (PreToolUseRequest, PostToolUseRequest, StopRequest, etc.)
> have session_id typed as str, forcing callers to write `session_id=str(session_id)`.
>
> The models should accept SessionID directly. Pydantic handles UUID serialization
> natively - no custom serializer needed. This eliminates repetitive str() conversions
> and provides type safety at the API boundary.
>
> **Note:** StopRequest session_id should accept SessionID directly

```
      87:     subprocess.run(["git", "commit", "-m", "Initial commit"], check=True)
      88:
      89:     # Create stop hook request
>>>   90:     request = StopRequest(hook_event_name="Stop", session_id=str(session_id))
      91:
      92:     # Handle the hook
      93:     result = handler.handle("Stop", request)
   ...
     140:     )
     141:
     142:     # Create stop hook request
>>>  143:     request = StopRequest(hook_event_name="Stop", session_id=str(session_id))
     144:
     145:     # Handle the hook
     146:     result = handler.handle("Stop", request)
```

### `request-session-id-typed-as-str.yaml` / `occ-4`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/request-session-id-typed-as-str.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/ducktape_llm_common/claude_linter_v2/cli.py#L216-L248)

File: `ducktape_llm_common/claude_linter_v2/cli.py` (L216, L248)

> Hook request models (PreToolUseRequest, PostToolUseRequest, StopRequest, etc.)
> have session_id typed as str, forcing callers to write `session_id=str(session_id)`.
>
> The models should accept SessionID directly. Pydantic handles UUID serialization
> natively - no custom serializer needed. This eliminates repetitive str() conversions
> and provides type safety at the API boundary.
>
> **Note:** session_allow and session_deny take session: str | None instead of SessionID | None

```
     213: @click.option("--session", type=str, help="Specific session ID (default: all in current dir)")
     214: @click.option("--dir", type=Path, help="Directory to affect (default: current)")
     215: def session_allow(predicate: str, expires: str | None, session: str | None, dir: Path | None) -> None:
>>>  216:     """Grant temporary permissions using Python predicates."""
     217:
     218:     manager = SessionManager()
     219:
   ...
     245: @click.option("--session", type=str, help="Specific session ID (default: all in current dir)")
     246: @click.option("--dir", type=Path, help="Directory to affect (default: current)")
     247: def session_deny(predicate: str, expires: str | None, session: str | None, dir: Path | None) -> None:
>>>  248:     """Deny permissions using Python predicates.
     249:
     250:     Examples:
     251:         cl2 session deny 'Write("/etc/*")'
```

### `session-id-parsing-duplication.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/session-id-parsing-duplication.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/ducktape_llm_common/claude_linter_v2/session/manager.py#L144-L166)

File: `ducktape_llm_common/claude_linter_v2/session/manager.py` (L144, L166)

> SessionManager has two occurrences of `SessionID(session_file.stem)` that directly
> construct a SessionID from a string without validation. The `parse_session_id()`
> function exists to validate UUID format before constructing SessionID.
>
> Pattern should be `parse_session_id(session_file.stem)` to ensure the stem is a
> valid UUID before wrapping it in SessionID.
>
> **Note:** Use parse_session_id() instead of direct SessionID() construction

```
     141:         else:
     142:             # Add to all sessions in the directory
     143:             for session_file in self.sessions_dir.glob("*.json"):
>>>  144:                 sid = SessionID(session_file.stem)
     145:                 session_data = self._load_session(sid)
     146:
     147:                 # Skip if session is in different directory
   ...
     163:
     164:         # Scan all session files
     165:         for session_file in self.sessions_dir.glob("*.json"):
>>>  166:             session_id = SessionID(session_file.stem)
     167:             session_data = self._load_session(session_id)
     168:
     169:             # Skip sessions in other directories unless requested
```

### `session-id-typed-as-string.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/session-id-typed-as-string.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/ducktape_llm_common/claude_linter_v2/session/manager.py#L33-L43)

File: `ducktape_llm_common/claude_linter_v2/session/manager.py` (L33-43)

> SessionData.id field is typed as str but should be typed as SessionID (UUID).
> Pydantic 2 supports UUID serialization/deserialization natively, so there's
> no need to store it as a string. Using the correct type provides type safety
> and avoids unnecessary str() conversions in tests and application code.
>
> **Note:** SessionData model has id field typed as str instead of SessionID (UUID)

```
      30:     expires: datetime | None = None
      31:
      32:
>>>   33: class SessionData(BaseModel):
>>>   34:     """Session data structure."""
>>>   35:
>>>   36:     model_config = ConfigDict(frozen=False, arbitrary_types_allowed=True)
>>>   37:
>>>   38:     id: str
>>>   39:     created: datetime
>>>   40:     last_seen: datetime | None = None
>>>   41:     directory: Path | None = None
>>>   42:     rules: list[Rule] = Field(default_factory=list)
>>>   43:     notification_id: int | None = None
      44:
      45:
      46: # Type alias for backwards compatibility
```

### `str-file-path-in-violations.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/str-file-path-in-violations.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/ducktape_llm_common/claude_linter_v2/session/violations.py#L34-L73)

File: `ducktape_llm_common/claude_linter_v2/session/violations.py` (L34, L60, L73)

> ViolationTracker methods take `file_path: str` instead of `file_path: Path`.
> Even though the paths are stored as strings in dicts, callers already have Path
> objects and must convert to str. The interface should accept Path and convert
> internally if needed for storage.
>
> Change signatures to `file_path: Path` and convert to str only at storage point.
>
> **Note:** add_violation, add_violations, mark_file_fixed all take file_path: str

```
      31:     def add_violation(
      32:         self,
      33:         session_id: SessionID,
>>>   34:         file_path: str,
      35:         line: int,
      36:         message: str,
      37:         severity: str = "error",
   ...
      57:         self._violations[session_id][key] = violation_dict
      58:
      59:     def add_violations(
>>>   60:         self, session_id: SessionID, violations: list[Violation], file_path: str, severity: str = "error"
      61:     ) -> None:
      62:         """Add multiple violations from a linter."""
      63:         for v in violations:
   ...
      70:                 rule=v.rule,
      71:             )
      72:
>>>   73:     def mark_file_fixed(self, session_id: SessionID, file_path: str) -> None:
      74:         """Mark all violations in a file as fixed."""
      75:         if session_id not in self._violations:
      76:             return
```

### `test-file-path-fixture-missing.yaml` / `occ-1`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/test-file-path-fixture-missing.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/tests/claude_linter_v2/test_python_formatter.py#L38-L194)

File: `tests/claude_linter_v2/test_python_formatter.py` (L38, L64, L88, L103, L136, L147, L166, L185, L194)

> Test files call analyze_code(), format_code(), and check_code() without a consistent
> file_path parameter. Multiple occurrences use:
>
> - No file_path at all
> - Inline strings like "/tmp/test.py"
> - Ad-hoc tmp_path constructions
>
> Should extract a shared TEST_FILE constant or fixture (e.g., Path("/test/file.py"))
> to use consistently across all tests. This provides:
>
> - Consistent behavior for path-dependent logic
> - Clear indication that tests are using synthetic paths
> - Single place to update if path handling changes
>
> **Note:** format_code() should pass file_path=TEST_FILE

```
      35:         formatter = PythonFormatter(["ruff"])
      36:         formatter._available_tools = ["ruff"]
      37:
>>>   38:         result, changes = formatter.format_code(input_code)
      39:
      40:         assert result == formatted_code
      41:         assert changes == ["Applied ruff formatting"]
   ...
      61:         formatter = PythonFormatter(["black"])
      62:         formatter._available_tools = ["black"]
      63:
>>>   64:         result, changes = formatter.format_code(input_code)
      65:
      66:         assert result == formatted_code
      67:         assert changes == ["Applied black formatting"]
   ...
      85:         formatter = PythonFormatter(["ruff"])
      86:         formatter._available_tools = ["ruff"]
      87:
>>>   88:         result, changes = formatter.format_code(code)
      89:
      90:         assert result == code
      91:         assert changes == []
   ...
     100:         formatter = PythonFormatter(["ruff"])
     101:         formatter._available_tools = ["ruff"]
     102:
>>>  103:         result, changes = formatter.format_code(code)
     104:
     105:         # Should return original code on error
     106:         assert result == code
   ...
     133:         formatter = PythonFormatter(["ruff"])
     134:         formatter._available_tools = ["ruff"]
     135:
>>>  136:         result, changes = formatter.format_code(input_code, categories=[AutofixCategory.IMPORTS])
     137:
     138:         assert result == fixed_code
     139:         assert "Fixed import ordering and removed unused imports" in changes
   ...
     144:         formatter._available_tools = []
     145:
     146:         code = "x=1+2"
>>>  147:         result, changes = formatter.format_code(code)
     148:
     149:         assert result == code
     150:         assert changes == []
   ...
     163:         formatter._apply_formatting = MagicMock(return_value=(code, []))
     164:         formatter._fix_imports = MagicMock(return_value=(code, []))
     165:
>>>  166:         formatter.format_code(code, categories=[AutofixCategory.ALL])
     167:
     168:         # Both methods should be called
     169:         formatter._apply_formatting.assert_called_once()
   ...
     182:         formatter._fix_imports = MagicMock(return_value=(code, []))
     183:
     184:         # Only formatting
>>>  185:         formatter.format_code(code, categories=[AutofixCategory.FORMATTING])
     186:         formatter._apply_formatting.assert_called_once()
     187:         formatter._fix_imports.assert_not_called()
     188:
   ...
     191:         formatter._fix_imports.reset_mock()
     192:
     193:         # Only imports
>>>  194:         formatter.format_code(code, categories=[AutofixCategory.IMPORTS])
     195:         formatter._apply_formatting.assert_not_called()
     196:         formatter._fix_imports.assert_called_once()
     197:
```

### `test-file-path-fixture-missing.yaml` / `occ-2`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/test-file-path-fixture-missing.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/tests/claude_linter_v2/test_python_ruff.py#L49-L198)

File: `tests/claude_linter_v2/test_python_ruff.py` (L49, L71, L101, L118, L145, L162, L175, L198)

> Test files call analyze_code(), format_code(), and check_code() without a consistent
> file_path parameter. Multiple occurrences use:
>
> - No file_path at all
> - Inline strings like "/tmp/test.py"
> - Ad-hoc tmp_path constructions
>
> Should extract a shared TEST_FILE constant or fixture (e.g., Path("/test/file.py"))
> to use consistently across all tests. This provides:
>
> - Consistent behavior for path-dependent logic
> - Clear indication that tests are using synthetic paths
> - Single place to update if path handling changes
>
> **Note:** check_code() should pass file_path=TEST_FILE

```
      46:         linter = PythonRuffLinter()
      47:         linter._ruff_available = True
      48:
>>>   49:         violations = linter.check_code(code)
      50:
      51:         assert len(violations) == 1
      52:         assert violations[0].rule == "ruff:E722"
   ...
      68:         linter = PythonRuffLinter()
      69:         linter._ruff_available = True
      70:
>>>   71:         violations = linter.check_code(code)
      72:
      73:         assert len(violations) == 0
      74:
   ...
      98:         linter = PythonRuffLinter()
      99:         linter._ruff_available = True
     100:
>>>  101:         violations = linter.check_code("code", critical_only=True)
     102:
     103:         # Should only return the critical violation
     104:         assert len(violations) == 1
   ...
     115:         linter = PythonRuffLinter(force_select=force_rules)
     116:         linter._ruff_available = True
     117:
>>>  118:         linter.check_code(code, critical_only=False)
     119:
     120:         # Verify the command included force-select rules
     121:         call_args = mock_run.call_args[0][0]
   ...
     142:         linter = PythonRuffLinter()
     143:         linter._ruff_available = True
     144:
>>>  145:         violations = linter.check_code("import unused", critical_only=False)
     146:
     147:         assert len(violations) == 1
     148:         assert violations[0].fixable is True
   ...
     159:         linter = PythonRuffLinter()
     160:         linter._ruff_available = True
     161:
>>>  162:         violations = linter.check_code("code")
     163:
     164:         # Should return empty list on error
     165:         assert violations == []
   ...
     172:         linter = PythonRuffLinter()
     173:         linter._ruff_available = True
     174:
>>>  175:         violations = linter.check_code("code")
     176:
     177:         # Should return empty list on parse error
     178:         assert violations == []
   ...
     195:         linter = PythonRuffLinter()
     196:         linter._ruff_available = False
     197:
>>>  198:         violations = linter.check_code("code")
     199:
     200:         assert violations == []
     201:
```

### `test-request-raw-dict-construction.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/test-request-raw-dict-construction.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/tests/claude_linter_v2/test_integration.py#L15-L219)

File: `tests/claude_linter_v2/test_integration.py` (L15-28, L46-58, L76-89, L112-125, L146-159, L191-204, L216-219)

> Tests construct PreToolUseRequest/PostToolUseRequest payloads as raw dicts:
> request_data = {"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {...}}
>
> Instead, use Pydantic models directly:
> request = PreToolUseRequest(
> session_id=session_id,
> hook_event_name="PreToolUse",
> tool_name="Write",
> tool_input={"file_path": ..., "content": ...},
> )
>
> Using typed models provides compile-time checking and makes test intent clearer.
>
> **Note:** request_data dict should be PreToolUseRequest model

```
      12:
      13:     def test_pre_hook_bare_except(self):
      14:         """Test that pre-hook blocks bare except."""
>>>   15:         request_data = {
>>>   16:             "hook_event_name": "PreToolUse",
>>>   17:             "tool_name": "Write",
>>>   18:             "tool_input": {
>>>   19:                 "file_path": "/tmp/test_bare_except.py",
>>>   20:                 "content": """
>>>   21: try:
>>>   22:     x = 1/0
>>>   23: except:
>>>   24:     pass
>>>   25: """,
>>>   26:             },
>>>   27:             "session_id": "12345678-1234-5678-1234-567812345678",
>>>   28:         }
      29:
      30:         result = subprocess.run(
      31:             [sys.executable, "-m", "ducktape_llm_common.claude_linter_v2.cli", "hook"],
   ...
      43:
      44:     def test_pre_hook_hasattr(self):
      45:         """Test that pre-hook blocks hasattr usage."""
>>>   46:         request_data = {
>>>   47:             "hook_event_name": "PreToolUse",
>>>   48:             "tool_name": "Write",
>>>   49:             "tool_input": {
>>>   50:                 "file_path": "/tmp/test_hasattr.py",
>>>   51:                 "content": """
>>>   52: obj = object()
>>>   53: if hasattr(obj, 'foo'):
>>>   54:     print("has foo")
>>>   55: """,
>>>   56:             },
>>>   57:             "session_id": "12345678-1234-5678-1234-567812345679",
>>>   58:         }
      59:
      60:         result = subprocess.run(
      61:             [sys.executable, "-m", "ducktape_llm_common.claude_linter_v2.cli", "hook"],
   ...
      73:
      74:     def test_pre_hook_clean_code(self):
      75:         """Test that pre-hook passes clean code."""
>>>   76:         request_data = {
>>>   77:             "hook_event_name": "PreToolUse",
>>>   78:             "tool_name": "Write",
>>>   79:             "tool_input": {
>>>   80:                 "file_path": "/tmp/test_clean.py",
>>>   81:                 "content": """
>>>   82: def hello():
>>>   83:     try:
>>>   84:         print("Hello, world!")
>>>   85:     except ValueError as e:
>>>   86:         print(f"Error: {e}")
>>>   87: """,
>>>   88:             },
>>>   89:             "session_id": "12345678-1234-5678-1234-567812345680",
      90:         }
      91:
      92:         result = subprocess.run(
   ...
     109:     )
     110:     def test_pre_hook_ruff_violation(self):
     111:         """Test that pre-hook blocks ruff violations."""
>>>  112:         request_data = {
>>>  113:             "hook_event_name": "PreToolUse",
>>>  114:             "tool_name": "Write",
>>>  115:             "tool_input": {
>>>  116:                 "file_path": "/tmp/test_mutable_default.py",
>>>  117:                 "content": """
>>>  118: import os
>>>  119:
>>>  120: def get_data():
>>>  121:     # Mutable default argument
>>>  122:     def process(items=[]):
>>>  123:         items.append(1)
>>>  124:         return items
>>>  125: """,
     126:             },
     127:             "session_id": "12345678-1234-5678-1234-567812345681",
     128:         }
   ...
     143:
     144:     def test_pre_hook_barrel_init(self):
     145:         """Test that pre-hook blocks barrel __init__.py."""
>>>  146:         request_data = {
>>>  147:             "hook_event_name": "PreToolUse",
>>>  148:             "tool_name": "Write",
>>>  149:             "tool_input": {
>>>  150:                 "file_path": "/tmp/__init__.py",
>>>  151:                 "content": """
>>>  152: from .module1 import *
>>>  153: from .module2 import Class1, Class2
>>>  154:
>>>  155: __all__ = ['Class1', 'Class2']
>>>  156: """,
>>>  157:             },
>>>  158:             "session_id": "12345678-1234-5678-1234-567812345682",
>>>  159:         }
     160:
     161:         result = subprocess.run(
     162:             [sys.executable, "-m", "ducktape_llm_common.claude_linter_v2.cli", "hook"],
   ...
     188:
     189:     def test_pre_hook_non_python_file(self):
     190:         """Test that pre-hook passes non-Python files."""
>>>  191:         request_data = {
>>>  192:             "hook_event_name": "PreToolUse",
>>>  193:             "tool_name": "Write",
>>>  194:             "tool_input": {
>>>  195:                 "file_path": "/tmp/test.txt",
>>>  196:                 "content": "This is just a text file with except: and hasattr",
>>>  197:             },
>>>  198:             "session_id": "12345678-1234-5678-1234-567812345683",
>>>  199:         }
>>>  200:
>>>  201:         result = subprocess.run(
>>>  202:             [sys.executable, "-m", "ducktape_llm_common.claude_linter_v2.cli", "hook"],
>>>  203:             input=json.dumps(request_data),
>>>  204:             capture_output=True,
     205:             text=True,
     206:             check=False,
     207:         )
   ...
     213:
     214:     def test_post_hook_basic(self):
     215:         """Test that post-hook runs without errors."""
>>>  216:         request_data = {
>>>  217:             "hook_event_name": "PostToolUse",
>>>  218:             "tool_name": "Write",
>>>  219:             "tool_input": {"file_path": "/tmp/test_post.py", "content": "x=1+2  # poorly formatted"},
     220:             "session_id": "12345678-1234-5678-1234-567812345684",
     221:         }
     222:
```

### `unnecessary-str-cast.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/unnecessary-str-cast.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/ducktape_llm_common/claude_linter_v2/checker.py#L50-L87)

File: `ducktape_llm_common/claude_linter_v2/checker.py` (L50, L73, L76, L81, L87)

> Redundant `str(file_path)` conversions when file_path is already Path and the
> receiving function/method accepts os.PathLike or when Path methods should be used
> directly. Python 3.13+ subprocess and ast.parse accept Path objects natively.
>
> Remove the str() casts and use Path methods (.suffix, .name) instead of string
> operations (.endswith).
>
> **Note:** Multiple str(file_path) casts in check_file method

```
      47:         violations: list[Violation] = []
      48:
      49:         # Only check Python files for now
>>>   50:         if not str(file_path).endswith(".py"):
      51:             return violations
      52:
      53:         try:
   ...
      70:                 or (getattr_config.enabled if getattr_config else False)
      71:                 or (setattr_config.enabled if setattr_config else False)
      72:             ),
>>>   73:             barrel_init=str(file_path).endswith("__init__.py")
      74:             and (barrel_init_config.enabled if barrel_init_config else False),
      75:         )
>>>   76:         ast_violations = analyzer.analyze_code(content, str(file_path))
      77:         violations.extend(ast_violations)
      78:
      79:         # Run ruff checks
      80:         ruff_linter = PythonRuffLinter(force_select=self.config.get_ruff_codes_to_select())
>>>   81:         ruff_violations = ruff_linter.check_code(content, str(file_path), critical_only=False)
      82:         violations.extend(ruff_violations)
      83:
      84:         # Apply fixes if requested
      85:         if self.fix and self.categories:
      86:             formatter = PythonFormatter(self.config.python_tools)
>>>   87:             formatted_content, changes = formatter.format_code(content, str(file_path), self.categories)
      88:
      89:             if changes and formatted_content != content:
      90:                 try:
```

### `unused-function-parameter.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/unused-function-parameter.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/ducktape_llm_common/claude_linter_v2/llm_analyzer.py#L62-L135)

File: `ducktape_llm_common/claude_linter_v2/llm_analyzer.py` (L135, L62)

> `_parse_llm_result(file_path: str)` accepts a `file_path` parameter that is never
> used in the function body. The parameter is passed at the call site (line 62) but
> the function doesn't reference it. Dead parameters add noise and mislead readers
> about what the function actually needs.
>
> Remove the unused parameter from both signature and call site.
>
> **Note:** file_path param in signature (135) and call site (62) - param unused in body

```
      59:             result = self._call_llm(prompt)
      60:
      61:             # Parse result
>>>   62:             is_ok, message, violations = self._parse_llm_result(result, file_path)
      63:
      64:             return is_ok, message, violations
      65:
   ...
     132:         # Mock response - always return OK for now
     133:         return {"ok": True, "violations": []}
     134:
>>>  135:     def _parse_llm_result(self, result: dict[str, Any], file_path: str) -> tuple[bool, str | None, list[Violation]]:
     136:         """Parse LLM result into our format."""
     137:         try:
     138:             is_ok = result.get("ok", True)
```

### `unused-mixin.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/unused-mixin.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/ducktape_llm_common/hook_session_state.py#L36-L104)

File: `ducktape_llm_common/hook_session_state.py` (L36-104)

> StatefulHookMixin (lines 36-104) is defined but never used anywhere in the codebase.
> Delete it or implement something that uses it.

```
      33:
      34:
      35: class StatefulHookMixin:
>>>   36:     """
>>>   37:     Mixin for hooks that need Pydantic-based session state.
>>>   38:
>>>   39:     Automatically loads state before hook dispatch and saves on destruction.
>>>   40:     Hook can access state via self.state.
>>>   41:
>>>   42:     Example:
>>>   43:         class MyHookState(BaseModel):
>>>   44:             tool_calls: int = 0
>>>   45:             blocked_files: list[str] = []
>>>   46:
>>>   47:         class MyHook(ClaudeCodeHookBase, StatefulHookMixin):
>>>   48:             hook_name = "my-security-hook"
>>>   49:             StateModel = MyHookState
>>>   50:
>>>   51:             def pre_tool_use(self, request: PreToolUseRequest) -> PreToolOutcome:
>>>   52:                 self.state.tool_calls += 1
>>>   53:                 return PreToolApprove()
>>>   54:     """
>>>   55:
>>>   56:     hook_name: str
>>>   57:     StateModel: type[StateModel]
>>>   58:
>>>   59:     def __init__(self, *args, **kwargs):
>>>   60:         super().__init__(*args, **kwargs)
>>>   61:         if not hasattr(self, "hook_name"):
>>>   62:             raise ValueError("StatefulHookMixin requires 'hook_name' class attribute")
>>>   63:         if not hasattr(self, "StateModel"):
>>>   64:             raise ValueError("StatefulHookMixin requires 'StateModel' class attribute")
>>>   65:
>>>   66:         self.state: StateModel = None
>>>   67:         self._current_session: SessionID = None
>>>   68:
>>>   69:     def _get_state_file(self, session_id: SessionID) -> Path:
>>>   70:         """Get the state file path for a session."""
>>>   71:         session_dir = get_session_dir(self.hook_name, session_id)
>>>   72:         return session_dir / "state.json"
>>>   73:
>>>   74:     def _load_state(self, session_id: SessionID) -> None:
>>>   75:         """Load state from file or create new instance."""
>>>   76:         state_file = self._get_state_file(session_id)
>>>   77:
>>>   78:         if state_file.exists():
>>>   79:             try:
>>>   80:                 self.state = self.StateModel.model_validate_json(state_file.read_text())
>>>   81:                 self._current_session = session_id
>>>   82:                 return
>>>   83:             except Exception:
>>>   84:                 # If corrupted, start fresh
>>>   85:                 pass
>>>   86:
>>>   87:         self.state = self.StateModel()
>>>   88:         self._current_session = session_id
>>>   89:
>>>   90:     def _save_state(self) -> None:
>>>   91:         """Save current state to file."""
>>>   92:         if self.state is not None and self._current_session is not None:
>>>   93:             state_file = self._get_state_file(self._current_session)
>>>   94:             state_file.write_text(self.state.model_dump_json(indent=2))
>>>   95:
>>>   96:     def dispatch_hook(self, request) -> any:
>>>   97:         """Override dispatch to load state before hook execution."""
>>>   98:         self._load_state(request.session_id)
>>>   99:         return super().dispatch_hook(request)
>>>  100:
>>>  101:     def __del__(self):
>>>  102:         """Auto-save state on destruction."""
>>>  103:         with contextlib.suppress(Exception):
>>>  104:             self._save_state()
```

### `verbose-example-messages.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/verbose-example-messages.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/ducktape_llm_common/claude_outcomes.py#L48-L55)

File: `ducktape_llm_common/claude_outcomes.py` (L48-55)

> Example llm_message strings in HookOutcome docstrings are unnecessarily verbose
> and multi-line. A one-liner like "Permission denied, can't edit production files"
> suffices to demonstrate usage.
>
> **Note:** PreToolDeny example has multi-line message with override instructions

```
      45:
      46: @dataclass
      47: class PreToolNoOpinion(HookOutcome):
>>>   48:     """No opinion - let existing permission flow decide."""
>>>   49:
>>>   50:     def to_claude_response(self) -> PreToolResponse:
>>>   51:         return PreToolResponse()  # undefined decision = existing permission flow
>>>   52:
>>>   53:
>>>   54: PreToolOutcome = PreToolApprove | PreToolDeny | PreToolNoOpinion
>>>   55:
      56:
      57: # PostToolUse Outcomes
      58: @dataclass
```

### `verbose-example-messages.yaml` / `occ-1`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/issues/verbose-example-messages.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape_llm_common/2026-01-03-00/code/ducktape_llm_common/claude_outcomes.py#L127-L135)

File: `ducktape_llm_common/claude_outcomes.py` (L127-135)

> Example llm_message strings in HookOutcome docstrings are unnecessarily verbose
> and multi-line. A one-liner like "Permission denied, can't edit production files"
> suffices to demonstrate usage.
>
> **Note:** StopPrevent example has multi-line error list

```
     124:     llm_message: str
     125:
     126:     def to_claude_response(self) -> StopResponse:
>>>  127:         return StopResponse(decision="block", reason=self.llm_message)
>>>  128:
>>>  129:
>>>  130: @dataclass
>>>  131: class StopAllowWithInfo(HookOutcome):
>>>  132:     """Allow Claude to end its turn, with an info message (non-blocking)."""
>>>  133:
>>>  134:     llm_message: str
>>>  135:
     136:     def to_claude_response(self) -> StopResponse:
     137:         return StopResponse()
     138:
```

## ducktape/2025-11-22-00 (19)

### `duplicate-style-definitions.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/duplicate-style-definitions.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/agent/web/src/components/AgentsSidebar.svelte#L347)

File: `adgn/src/adgn/agent/web/src/components/AgentsSidebar.svelte` (L347)

> AgentsSidebar.svelte line 347 contains a useless historical comment: "Backdrop
> styling moved to ModalBackdrop component". This documents a past refactoring
> rather than explaining current behavior.
>
> Problems: (1) historical note provides no value to readers, (2) ModalBackdrop's
> existence is already obvious from imports and usage, (3) redundant with "Modal
> styles" section header.
>
> Delete the comment. Historical notes ("moved to...", "used to be...") clutter
> code without explaining current behavior. Comments should explain complexity,
> workarounds, or non-obvious behavior, not document past refactorings.

```
     344:   .row { display: flex; gap: 0.5rem; align-items: center; }
     345:   .preset { flex: 1; min-width: 0; }
     346:   /* Modal styles */
>>>  347:   /* Backdrop styling moved to ModalBackdrop component */
     348:   .modal { background: var(--surface); color: var(--text); min-width: 320px; max-width: 90vw; border: 1px solid var(--border); border-radius: 6px; box-shadow: 0 8px 24px rgba(0,0,0,0.25); }
     349:   .modal header { padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--border); font-weight: 600; }
     350:   .modal .body { padding: 0.75rem; display: grid; grid-template-columns: 1fr; gap: 0.5rem; }
```

### `duplicate-transcript-files.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/duplicate-transcript-files.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/agent/transcript_handler.py#L38-L57)

File: `adgn/src/adgn/agent/transcript_handler.py` (L38-39, L41-42, L53-57)

> `TranscriptHandler` writes the same events to two nearly-identical files: `events.jsonl`
> (with timestamps) and `transcript.jsonl` (without timestamps). Lines 38-39 define both
> paths, lines 53-57 write to both files on every event.
>
> **Fix:** Choose one format. Keep the timestamped format (`events.jsonl`) as primary since
> it preserves temporal information (timestamps are useful for debugging, analysis, replay;
> you can strip them if needed but can't add them back). Remove `_transcript_path` and the
> second write. If both formats are needed, generate the compact format on-demand from the
> timestamped one via an `export_compact_transcript()` method. Benefits: single source of
> truth, half the I/O, no redundant data, easier maintenance.

```
      35:     def __init__(self, *, dest_dir: Path) -> None:
      36:         self._root = dest_dir
      37:         self._root.mkdir(parents=True, exist_ok=True)
>>>   38:         self._events_path = self._root / "events.jsonl"
>>>   39:         self._transcript_path = self._root / "transcript.jsonl"
      40:         # Fail fast if a transcript already exists at destination
>>>   41:         if self._events_path.exists():
>>>   42:             raise FileExistsError(f"Transcript already exists: {self._events_path}")
      43:         # Write a small metadata file once
      44:         (self._root / "metadata.json").write_text(
      45:             json.dumps({"started": datetime.utcnow().isoformat() + "Z"}, indent=2), encoding="utf-8"
   ...
      50:         rec = to_jsonl_record(evt)
      51:         # Timestamped envelope (events.jsonl)
      52:         out = {"ts": datetime.utcnow().isoformat() + "Z", **rec}
>>>   53:         with self._events_path.open("a", encoding="utf-8") as f:
>>>   54:             f.write(json.dumps(out, ensure_ascii=False) + "\n")
>>>   55:         # Compact transcript (transcript.jsonl)
>>>   56:         with self._transcript_path.open("a", encoding="utf-8") as g:
>>>   57:             g.write(json.dumps(rec, ensure_ascii=False) + "\n")
      58:
      59:     # ---- BaseHandler hooks (typed) ----
      60:     def on_user_text_event(self, evt: UserText) -> None:
```

### `duplicated-agent-info.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/duplicated-agent-info.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/agent/mcp_bridge/server.py#L187-L302)

File: `adgn/src/adgn/agent/mcp_bridge/server.py` (L252-279, L285-302, L187-198)

> The code has two problems in server.py:
>
> **Problem 1: Duplicated agent info construction**
>
> Both `list_agents()` and `get_agent_info()` build the same `AgentInfo` object with identical
> logic (determine run phase, check mode, build capabilities), but the implementation is
> duplicated line-by-line instead of extracting a shared helper (server.py, lines 252-302).
>
> **The correct approach:**
> Extract a `_build_agent_info(agent_id, agent)` helper method and call it from both resources.
> Alternatively, have `list_agents` call `get_agent_info` for each agent.
>
> **Problem 2: Thin wrapper methods**
>
> Methods `get_infrastructure()`, `get_agent_mode()`, and `get_local_runtime()` are trivial
> wrappers that just call `_get_agent_or_raise()` and access one field (server.py, lines 187-198).
>
> **The correct approach:**
> Let callers use `_get_agent_or_raise()` directly and access fields themselves
> (`agent.running`, `agent.mode`, `agent.local_runtime`). Or if public access is needed,
> rename `_get_agent_or_raise` to `get_agent` and let callers access fields directly.

```
     184:             raise KeyError(f"Agent {agent_id} not yet initialized")
     185:         return agent
     186:
>>>  187:     async def get_infrastructure(self, agent_id: AgentID) -> RunningInfrastructure:
>>>  188:         """Get infrastructure. Raises KeyError if not found."""
>>>  189:         return self._get_agent_or_raise(agent_id).running
>>>  190:
>>>  191:     def get_agent_mode(self, agent_id: AgentID) -> AgentMode:
>>>  192:         """Get agent mode. Raises KeyError if not found."""
>>>  193:         return self._get_agent_or_raise(agent_id).mode
>>>  194:
>>>  195:     def get_local_runtime(self, agent_id: AgentID) -> LocalAgentRuntime | None:
>>>  196:         """Get local runtime or None if bridge agent. Raises KeyError if not found."""
>>>  197:         return self._get_agent_or_raise(agent_id).local_runtime
>>>  198:
     199:     def register_local_agent(
     200:         self,
     201:         agent_id: AgentID,
   ...
     249:
     250:     def _register_resources(self) -> None:
     251:         @self.resource("resource://agents/list", name="agents_list", mime_type="application/json")
>>>  252:         async def list_agents() -> AgentsListResponse:
>>>  253:             """List all agents with detailed status."""
>>>  254:             agents = []
>>>  255:             for agent_id, entry in self._agents.items():
>>>  256:                 if entry.agent is None:
>>>  257:                     continue  # Skip uninitialized agents
>>>  258:
>>>  259:                 agent = entry.agent
>>>  260:
>>>  261:                 # Get infrastructure if available
>>>  262:                 infra = agent.running
>>>  263:                 live = infra is not None
>>>  264:
>>>  265:                 # Determine run phase and pending approvals
>>>  266:                 run_phase, pending_approvals = self._determine_run_phase(infra)
>>>  267:
>>>  268:                 # Determine capabilities
>>>  269:                 is_local = agent.mode == AgentMode.LOCAL
>>>  270:
>>>  271:                 agents.append(
>>>  272:                     AgentInfo(
>>>  273:                         id=agent_id,
>>>  274:                         mode=agent.mode,
>>>  275:                         live=live,
>>>  276:                         run_phase=run_phase,
>>>  277:                         pending_approvals=pending_approvals,
>>>  278:                         capabilities=AgentCapabilities(chat=is_local, agent_loop=is_local),
>>>  279:                     )
     280:                 )
     281:
     282:             return AgentsListResponse(agents=agents)
     283:
     284:         @self.resource("resource://agents/{agent_id}/info", name="agent_info", mime_type="application/json")
>>>  285:         async def get_agent_info(agent_id: AgentID) -> AgentInfo:
>>>  286:             """Get detailed information about a specific agent."""
>>>  287:             agent = self._get_agent_or_raise(agent_id)
>>>  288:
>>>  289:             infra = agent.running
>>>  290:             live = infra is not None
>>>  291:
>>>  292:             # Determine run phase and pending approvals
>>>  293:             run_phase, pending_approvals = self._determine_run_phase(infra)
>>>  294:
>>>  295:             is_local = agent.mode == AgentMode.LOCAL
>>>  296:
>>>  297:             return AgentInfo(
>>>  298:                 id=agent_id,
>>>  299:                 mode=agent.mode,
>>>  300:                 live=live,
>>>  301:                 run_phase=run_phase,
>>>  302:                 pending_approvals=pending_approvals,
     303:                 capabilities=AgentCapabilities(chat=is_local, agent_loop=is_local),
     304:             )
     305:
```

### `duplicated-notification-data.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/duplicated-notification-data.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/agent/notifications/types.py#L14-L49)

File: `adgn/src/adgn/agent/notifications/types.py` (L14-30, L33-49)

> notifications/types.py duplicates data in two ways: (1) NotificationsBatch
> (lines 14-30) stores both parsed fields (resources_updated, resource_list_changed)
> and raw MCP notifications, creating redundancy and unclear source of truth;
> (2) NotificationsBatch and NotificationsForModel (lines 33-51) represent the same
> data in different shapes (flat lists vs grouped by server).
>
> Problems: Parsed fields are derivable from raw, creating sync risk. Two classes
> for the same data. Manual deduplication. No single source of truth.
>
> Replace with single grouped representation: one class with dict[server, notices],
> parse once at construction via from_raw() classmethod, use frozenset for
> deduplication. Remove NotificationsForModel entirely.
>
> Benefits: Single source of truth (derived from raw on construction), no
> duplication, efficient lookups (grouped by server), helper methods for access
> patterns.
>
> Principle: Store data in ONE efficient representation, derive views on-demand.

```
      11:     uri: str = Field(description="Resource URI string for the update")
      12:
      13:
>>>   14: class NotificationsBatch(BaseModel):
>>>   15:     """Buffered notifications ready to be injected as model input or observed by UI.
>>>   16:
>>>   17:     Fields
>>>   18:     - resources_updated: derived per-update events with server+URI
>>>   19:     - resource_list_changed: list of server names where resources/list changed
>>>   20:     - raw: full MCP notification payloads captured for display/debugging
>>>   21:     """
>>>   22:
>>>   23:     resources_updated: list[ResourceUpdateEvent] = Field(
>>>   24:         default_factory=list, description="Derived resource update events (server, uri, version)"
>>>   25:     )
>>>   26:     resource_list_changed: list[str] = Field(default_factory=list, description="Servers with resources/list changed")
>>>   27:     # Raw MCP server notifications captured (only resources notifications are buffered here)
>>>   28:     raw: list[mcp_types.ResourceUpdatedNotification | mcp_types.ResourceListChangedNotification] = Field(
>>>   29:         default_factory=list, description="Full MCP resources notifications captured for display/debugging"
>>>   30:     )
      31:
      32:
>>>   33: class ResourcesServerNotice(BaseModel):
>>>   34:     """Per-server resources notice.
>>>   35:
>>>   36:     - updated: list of resource URIs updated for this server
>>>   37:     - list_changed: whether a resources/list_changed occurred for this server (best effort)
>>>   38:     """
>>>   39:
>>>   40:     updated: list[str] = Field(default_factory=list)
>>>   41:     list_changed: bool = False
>>>   42:
>>>   43:
>>>   44: class NotificationsForModel(BaseModel):
>>>   45:     """Top-level structured notification envelope used for message injection."""
>>>   46:
>>>   47:     resources: dict[str, ResourcesServerNotice] = Field(
>>>   48:         default_factory=dict, description="Per-server resources notice: {server -> {updated, list_changed}}"
>>>   49:     )
```

### `duplicated-xdg-paths.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/duplicated-xdg-paths.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/agent/mcp_bridge/cli.py#L36)

File: `adgn/src/adgn/agent/mcp_bridge/cli.py` (L36)

> The code constructs XDG user data directory paths (using `user_data_dir("adgn", ...)`)
> in multiple places instead of defining these paths once in a central location.
>
> **Current implementation:** Each module independently calls `user_data_dir("adgn", "agentydragon")`
> and constructs paths like `DEFAULT_DB_PATH = Path(...) / "mcp-bridge.db"` (mcp_bridge/cli.py, line 36).
>
> **The correct approach:**
> Create a central paths module (e.g., `adgn/paths.py`) that defines XDG directories once
> (USER_DATA_DIR, USER_CACHE_DIR, USER_CONFIG_DIR) and specific application paths
> (MCP_BRIDGE_DB, RESPONSES_CACHE_DB, AUTH_TOKENS_FILE, etc.). Import these constants
> throughout the codebase.

```
      33: logger = logging.getLogger(__name__)
      34:
      35: # Default database path in XDG user data directory
>>>   36: DEFAULT_DB_PATH = Path(user_data_dir("adgn", "agentydragon")) / "mcp-bridge.db"
      37:
      38:
      39: @click.group()
```

### `explicit-constructions-ui.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/explicit-constructions-ui.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/agent/web/src/components/GlobalApprovalsList.svelte#L29-L121)

File: `adgn/src/adgn/agent/web/src/components/GlobalApprovalsList.svelte` (L29-36, L115-121)

> Lines 29-36 define `parseArgs()` using manual `JSON.parse()` that returns `{}` on error (silent
> failure). Lines 115-121 manually parse approval blocks with `JSON.parse()`, destructuring
> `agent_id`, `tool_call`, `timestamp` without validation.
>
> Manual JSON parsing loses: (1) validation (accepts any JSON structure), (2) type safety
> (`Record<string, unknown>` doesn't match actual shape), (3) error visibility (parseArgs
> silently returns empty object), (4) schema checking (can't detect missing/extra fields).
>
> Backend has `ToolCall` Pydantic model (agent/types.py:20-25) with `name`, `call_id`,
> `args_json` fields. Frontend should use Zod schemas generated from Pydantic models via
> `adgn/scripts/generate_types.py` (commit 7c6cae7ad) extended with `json-schema-to-zod`.
>
> Replace `JSON.parse()` with `PendingApprovalSchema.parse(data)` for runtime validation,
> detailed error messages, and single source of truth (Backend Pydantic → Frontend Zod).

```
      26:   /**
      27:    * Parse tool call args_json to object
      28:    */
>>>   29:   function parseArgs(argsJson: string | null): Record<string, unknown> {
>>>   30:     if (!argsJson) return {}
>>>   31:     try {
>>>   32:       return JSON.parse(argsJson)
>>>   33:     } catch {
>>>   34:       return {}
>>>   35:     }
>>>   36:   }
      37:
      38:   // Group approvals by agent_id for display
      39:   $: groupedApprovals = approvals.reduce((acc, approval) => {
   ...
     112:     try {
     113:       // Read the global approvals resource
     114:       const contents = await readResource(mcpClient, MCPUris.approvalsPendingUri)
>>>  115:
>>>  116:       // Parse contents - it returns an array of TextResourceContents
>>>  117:       // Each block has: { uri, mimeType, text }
>>>  118:       // The text field contains JSON with: { agent_id, tool_call: { name, call_id, args_json }, timestamp }
>>>  119:       const parsedApprovals: Array<PendingApproval & { agent_id: string }> = []
>>>  120:
>>>  121:       for (const block of contents) {
     122:         if ('text' in block && block.mimeType === 'application/json') {
     123:           try {
     124:             const data = JSON.parse(block.text)
```

### `json-output-constraint.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/json-output-constraint.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/agent/policy_eval/runner.py#L80)

File: `adgn/src/adgn/agent/policy_eval/runner.py` (L80)

> Line 80 uses `.strip().splitlines()[-1]` to extract the last line, which unnecessarily constrains the policy
> output to
> not contain newlines in the JSON. Valid JSON can span multiple lines.
>
> **Current implementation assumes:**
>
> - Policy output is line-based
> - JSON response is on the last line
> - JSON can't contain newlines
>
> **Why this is problematic:**
>
> Valid pretty-printed JSON output would break:
>
> ```json
> {
>   "decision": "allow",
>   "rationale": "Looks good"
> }
> ```
>
> Gets parsed as: `json.loads('"rationale": "Looks good"\n}')` → Error!
>
> **Correct approach:**
>
> Parse the entire output directly (ideally policy should output ONLY JSON, not mix debug output and JSON. If
> debug
> output
> is needed, send it to stderr, not stdout):
>
> ```python
> try:
>     return PolicyResponse.model_validate_json(logs.strip())
> except Exception as e:
>     text = logs.decode("utf-8", errors="replace")
>     raise RuntimeError(f"invalid JSON from policy eval: {e}; output={text!r}") from e
> ```

```
      77:             data = json.loads(text.strip().splitlines()[-1]) if text.strip() else {}
      78:         except Exception as e:
      79:             raise RuntimeError(f"invalid JSON from policy eval: {e}; output={text!r}") from e
>>>   80:         return PolicyResponse.model_validate(data)
      81:     finally:
      82:         try:
      83:             container.remove(force=True)
```

### `manual-dict-parsing.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/manual-dict-parsing.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/agent/persist/events.py#L47-L100)

File: `adgn/src/adgn/agent/persist/events.py` (L47-50, L67-100)

> The `parse_event()` function manually parses event dictionaries using if-elif
> chains that inspect the `type` field and construct the appropriate payload class.
> This is exactly what Pydantic's discriminated union parsing does automatically,
> but the code reimplements it by hand.
>
> **Current implementation (events.py, lines 67-100):**
> The code defines `TypedPayload` with `Field(discriminator=None)` and implements
> a 30+ line `parse_event()` function with manual if-elif dispatching for each
> event type (USER_TEXT, ASSISTANT_TEXT, TOOL_CALL, etc.), manually extracting
> fields from dictionaries and constructing payload objects.
>
> **Problems:**
>
> 1. **Reimplements Pydantic**: Manual if-elif dispatching duplicates what Pydantic does
> 2. **No validation**: Manual `str()` casts and `.get()` don't validate structure
> 3. **Misleading type hint**: `Field(discriminator=None)` suggests discriminated union but doesn't use it
>
> **The correct approach:**
>
> Use Pydantic's discriminated union parsing: add `Literal["type"]` to each
> payload class, set `Field(discriminator="type")` on the union, and use
> `model_validate()`. This reduces the 30+ line manual parser to a 3-line
> function that injects the type field into the payload dict before validation.

```
      44:
      45: TypedPayload = Annotated[
      46:     UserTextPayload
>>>   47:     | AssistantTextPayload
>>>   48:     | ToolCallPayload
>>>   49:     | FunctionCallOutputPayload
>>>   50:     | ReasoningPayload
      51:     | ResponsePayload,
      52:     Field(discriminator=None),
      53: ]
   ...
      64:     model_config = ConfigDict(extra="forbid")
      65:
      66:
>>>   67: def parse_event(d: dict[str, Any]) -> EventRecord:
>>>   68:     raw_type = d.get("type")
>>>   69:     et = EventType(str(raw_type))
>>>   70:     seq = int(d.get("seq", 0))
>>>   71:     ts_raw = d.get("ts")
>>>   72:     ts = ts_raw if isinstance(ts_raw, datetime) else datetime.fromisoformat(str(ts_raw))
>>>   73:     call_id = d.get("call_id")
>>>   74:     tool_key = d.get("tool_key")
>>>   75:     payload_raw = d.get("payload") or {}
>>>   76:
>>>   77:     payload: TypedPayload
>>>   78:     if et == EventType.USER_TEXT:
>>>   79:         payload = UserTextPayload(text=str(payload_raw.get("text", "")))
>>>   80:     elif et == EventType.ASSISTANT_TEXT:
>>>   81:         payload = AssistantTextPayload(text=str(payload_raw.get("text", "")))
>>>   82:     elif et == EventType.TOOL_CALL:
>>>   83:         payload = ToolCallPayload(
>>>   84:             name=str(payload_raw.get("name", "")),
>>>   85:             args_json=payload_raw.get("args_json"),
>>>   86:             call_id=str(payload_raw.get("call_id") or d.get("call_id") or ""),
>>>   87:         )
>>>   88:     elif et == EventType.FUNCTION_CALL_OUTPUT:
>>>   89:         # Persisted payload is the Pydantic MCP CallToolResult JSON (alias field names)
>>>   90:         result = TypeAdapter(mcp_types.CallToolResult).validate_python(payload_raw)
>>>   91:         payload = FunctionCallOutputPayload(call_id=str(d.get("call_id") or ""), result=result)
>>>   92:     elif et == EventType.REASONING:
>>>   93:         payload = ReasoningPayload(text=str(payload_raw.get("text", "")))
>>>   94:     elif et == EventType.RESPONSE:
>>>   95:         payload = ResponsePayload(content=payload_raw)
>>>   96:     else:
>>>   97:         # Fallback to response-like envelope
>>>   98:         payload = ResponsePayload(content=payload_raw)
>>>   99:
>>>  100:     return EventRecord(seq=seq, ts=ts, type=et, payload=payload, call_id=call_id, tool_key=tool_key)
     101:
     102:
     103: def parse_events(items: list[dict[str, Any]]) -> list[EventRecord]:
```

### `manual-indentation-loop.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/manual-indentation-loop.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/git_commit_ai/cli.py#L573-L574)

File: `adgn/src/adgn/git_commit_ai/cli.py` (L573-574)

> Manual loop to indent lines instead of using `textwrap.indent()` from standard library.
>
> **Current code (cli.py:573-574):**
>
> ```python
> for line in previous_message.splitlines():
>     final_text += f"# {line}\n"
> ```
>
> **Correct approach:**
>
> ```python
> final_text += textwrap.indent(previous_message, "# ", lambda line: True)
> ```

```
     570:     final_text = msg
     571:     if previous_message:
     572:         final_text += "\n\n# Previous commit message (being amended):\n"
>>>  573:         for line in previous_message.splitlines():
>>>  574:             final_text += f"# {line}\n"
     575:     final_text += stats_comment + build_commit_template(repo, passthru)
     576:
     577:     commit_msg_path = Path(repo.path) / "COMMIT_EDITMSG"
```

### `manual-init-not-dataclass.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/manual-init-not-dataclass.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/agent/policy_eval/container.py#L17-L46)

File: `adgn/src/adgn/agent/policy_eval/container.py` (L17-46)

> Class (container.py:17-46) uses manual `__init__` for simple field
> initialization. The constructor does assignment-only initialization
> with no complex logic, perfect candidate for `@dataclass`.
>
> Benefits of dataclass: less boilerplate (no manual assignments), free
> `__repr__` for debugging, free `__eq__` for testing, type annotations
> serve as field declarations, standard Python idiom for data-holding
> classes. Use `__post_init__` if complex initialization needed.

```
      14: logger = logging.getLogger(__name__)
      15:
      16:
>>>   17: class ContainerPolicyEvaluator:
>>>   18:     """Evaluate policy decisions inside a one-off Docker container (isolated).
>>>   19:
>>>   20:     The active policy source is executed directly via `python -c <source>`; no
>>>   21:     per-agent volumes are required. The image must have the `adgn` package
>>>   22:     installed so the policy can import helpers. Network is disabled; no RW
>>>   23:     mounts; no container reuse.
>>>   24:     """
>>>   25:
>>>   26:     def __init__(
>>>   27:         self,
>>>   28:         *,
>>>   29:         agent_id: AgentID,
>>>   30:         docker_client: DockerClient,
>>>   31:         engine: ApprovalPolicyEngine,
>>>   32:         image: str | None = None,
>>>   33:         timeout_secs: float | None = None,
>>>   34:     ) -> None:
>>>   35:         if not agent_id:
>>>   36:             raise ValueError("ContainerPolicyEvaluator requires agent_id")
>>>   37:         self.agent_id = agent_id
>>>   38:         self.image: str = image or resolve_runtime_image()
>>>   39:         self.timeout_secs = (
>>>   40:             timeout_secs if timeout_secs is not None else float(os.getenv("ADGN_POLICY_EVAL_TIMEOUT_SECS", "5"))
>>>   41:         )
>>>   42:         self._docker = docker_client
>>>   43:         self._engine = engine
>>>   44:
>>>   45:     async def decide(self, policy_input: PolicyRequest) -> PolicyResponse:
>>>   46:         """Evaluate using the current policy source via run_policy_source."""
      47:         payload = {"name": policy_input.name, "arguments": policy_input.arguments}
      48:         policy_src, _ver = self._engine.get_policy()
      49:         return run_policy_source(
```

### `manual-isinstance-validation.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/manual-isinstance-validation.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/agent/mcp_bridge/auth.py#L60-L69)

File: `adgn/src/adgn/agent/mcp_bridge/auth.py` (L60-69)

> The `reload()` method manually validates that the loaded JSON is a dict with string
> keys and values using `isinstance()` checks, but this can be done automatically and
> more robustly using Pydantic's `TypeAdapter`.
>
> **Current implementation:** Manual validation loop checking isinstance on each token/agent_id
> pair, raising generic ValueError on mismatch (auth.py, lines 60-69).
>
> **The correct approach:**
> Use Pydantic's `TypeAdapter(dict[str, AgentID])` to validate and parse in one step.
> Can call `validate_python(data)` after `json.loads()` or `validate_json(text)` directly.

```
      57:             raise FileNotFoundError(f"Token mapping file not found: {self.path}")
      58:
      59:         data = json.loads(self.path.read_text())
>>>   60:         if not isinstance(data, dict):
>>>   61:             raise ValueError("Token mapping must be a JSON object")
>>>   62:
>>>   63:         # Validate all values are strings and convert to AgentID
>>>   64:         mapping: dict[str, AgentID] = {}
>>>   65:         for token, agent_id in data.items():
>>>   66:             if not isinstance(token, str) or not isinstance(agent_id, str):
>>>   67:                 raise ValueError(f"Invalid mapping: {token} -> {agent_id}")
>>>   68:             mapping[token] = AgentID(agent_id)
>>>   69:
      70:         self._mapping = mapping
      71:         logger.info(f"Loaded {len(self._mapping)} token mappings from {self.path}")
      72:
```

### `manual-json-parsing.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/manual-json-parsing.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/agent/policy_eval/runner.py#L76-L83)

File: `adgn/src/adgn/agent/policy_eval/runner.py` (L76-83)

> Line 80 does `json.loads(...)` to parse JSON, then passes the dict to `PolicyResponse.model_validate(data)`.
> Pydantic provides `model_validate_json()` which does both steps in one call and is more efficient.
>
> **Correct approach:**
>
> Use `model_validate_json()` directly on bytes:
>
> ```python
> logs = container.logs(stdout=True, stderr=True) or b""
> if status != 0:
>     text = logs.decode("utf-8", errors="replace")
>     raise RuntimeError(f"policy eval failed (exit={status}): {text.strip()}")
> try:
>     return PolicyResponse.model_validate_json(logs.strip())
> except Exception as e:
>     text = logs.decode("utf-8", errors="replace")
>     raise RuntimeError(f"invalid JSON from policy eval: {e}; output={text!r}") from e
> ```

```
      73:         text = logs.decode("utf-8", errors="replace")
      74:         if status != 0:
      75:             raise RuntimeError(f"policy eval failed (exit={status}): {text.strip()}")
>>>   76:         try:
>>>   77:             data = json.loads(text.strip().splitlines()[-1]) if text.strip() else {}
>>>   78:         except Exception as e:
>>>   79:             raise RuntimeError(f"invalid JSON from policy eval: {e}; output={text!r}") from e
>>>   80:         return PolicyResponse.model_validate(data)
>>>   81:     finally:
>>>   82:         try:
>>>   83:             container.remove(force=True)
      84:         except (docker.errors.APIError, docker.errors.NotFound) as e:
      85:             logger.warning("policy eval container cleanup failed", exc_info=e)
```

### `mixed-exit-code-conventions.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/mixed-exit-code-conventions.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/git_commit_ai/cli.py#L551-L732)

File: `adgn/src/adgn/git_commit_ai/cli.py` (L551-556, L558-564, L567-609, L728-732)

> Functions inconsistently mix two conventions for signaling exit codes: some declare
> `-> int` return types but actually raise `ExitWithCode` exceptions on error paths.
>
> **Evidence:**
>
> - `_commit_immediately` (lines 558-564): declared `-> int`, but raises `ExitWithCode(1)` on some paths
> - `_run_editor_flow` (lines 567-609): declared `-> int`, but has 7 paths that raise `ExitWithCode`
> - Callers (lines 728-732): expect int return, but `sys.exit(code)` is unreachable when exception raised
>
> **Problems:**
>
> 1. Type lies: functions promise `-> int` but raise exceptions, violating contracts
> 2. Unreachable code: code after exception-raising calls never executes
>
> **Fix:** Pick ONE convention consistently. Option A (always raise exceptions, change
> signatures to `-> None`): impossible to forget, clear failure paths, consistent with
> Python's `SystemExit`. Option B (always return int): never raise, always return codes.
> Mixing both violates type contracts and creates ad-hoc error handling.

```
     548:     )
     549:
     550:
>>>  551: class ExitWithCode(Exception):  # noqa: N818
>>>  552:     # TODO: Reconsider whether signalling exit codes via exceptions is the best approach
>>>  553:     def __init__(self, code: int):
>>>  554:         super().__init__(str(code))
>>>  555:         self.code = code
>>>  556:
     557:
>>>  558: async def _commit_immediately(msg: str, passthru: list[str]) -> int:
>>>  559:     if not msg.strip():
>>>  560:         print("Aborting commit due to empty AI commit message.", file=sys.stderr)
>>>  561:         raise ExitWithCode(1)
>>>  562:     commit_passthru = filter_commit_passthru(passthru)
>>>  563:     commit_proc = await asyncio.create_subprocess_exec("git", "commit", "-m", msg, "--no-verify", *commit_passthru)
>>>  564:     return await commit_proc.wait()
     565:
     566:
>>>  567: async def _run_editor_flow(
>>>  568:     repo: pygit2.Repository, msg: str, previous_message: str | None, stats_comment: str, passthru: list[str]
>>>  569: ) -> int:
>>>  570:     final_text = msg
>>>  571:     if previous_message:
>>>  572:         final_text += "\n\n# Previous commit message (being amended):\n"
>>>  573:         for line in previous_message.splitlines():
>>>  574:             final_text += f"# {line}\n"
>>>  575:     final_text += stats_comment + build_commit_template(repo, passthru)
>>>  576:
>>>  577:     commit_msg_path = Path(repo.path) / "COMMIT_EDITMSG"
>>>  578:     commit_msg_path.write_text(final_text)
>>>  579:
>>>  580:     mtime_before = commit_msg_path.stat().st_mtime
>>>  581:     content_before = final_text
>>>  582:
>>>  583:     editor = await _get_editor()
>>>  584:     editor_proc = await asyncio.create_subprocess_shell(f"{editor} {commit_msg_path}")
>>>  585:     if (rc := await editor_proc.wait()) != 0:
>>>  586:         print(f"Aborting commit: editor exited with code {rc} (e.g., :cq)", file=sys.stderr)
>>>  587:         raise ExitWithCode(1)
>>>  588:
>>>  589:     try:
>>>  590:         final_content = commit_msg_path.read_text()
>>>  591:         mtime_after = commit_msg_path.stat().st_mtime
>>>  592:         saved = mtime_after != mtime_before
>>>  593:         changed = final_content.rstrip("\n") != content_before
>>>  594:         if not saved and not changed:
>>>  595:             print("Aborting commit: editor closed without saving (unchanged commit message).", file=sys.stderr)
>>>  596:             raise ExitWithCode(1)
>>>  597:     except FileNotFoundError:
>>>  598:         print("Aborting commit.", file=sys.stderr)
>>>  599:         raise ExitWithCode(1)
>>>  600:
>>>  601:     content_lines: list[str] = []
>>>  602:     for line in final_content.splitlines():
>>>  603:         if line.startswith(SCISSORS_MARK):
>>>  604:             break
>>>  605:         if line.strip() and not line.strip().startswith("#"):
>>>  606:             content_lines.append(line)
>>>  607:     if not content_lines:
>>>  608:         print("Aborting commit due to empty commit message.", file=sys.stderr)
>>>  609:         raise ExitWithCode(1)
     610:
     611:     commit_passthru = filter_commit_passthru(passthru)
     612:     commit_proc = await asyncio.create_subprocess_exec(
   ...
     725:         stats_comment = _make_stats_comment(cached, diff, msg, elapsed_s)
     726:
     727:         if args.accept_ai:
>>>  728:             code = await _commit_immediately(msg, passthru)
>>>  729:             sys.exit(code)
>>>  730:
>>>  731:         code = await _run_editor_flow(repo, msg, previous_message, stats_comment, passthru)
>>>  732:         sys.exit(code)
     733:     except ExitWithCode as e:
     734:         sys.exit(e.code)
     735:
```

### `raw-sql-instead-of-orm.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/raw-sql-instead-of-orm.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/agent/persist/sqlite.py#L145-L165)

File: `adgn/src/adgn/agent/persist/sqlite.py` (L145-165)

> Query (sqlite.py:145-165) uses raw SQL with `text()` instead of SQLAlchemy
> ORM constructs. The function executes a SELECT with GROUP BY and COALESCE
> using string-based column references.
>
> Problems with raw SQL: not type-safe (columns as strings), not portable
> (SQL syntax varies), hard to maintain (refactoring tools don't track
> renames), poor error messages (runtime vs import time), no IDE navigation.
>
> Fix: use SQLAlchemy ORM with `session.query(Run.agent_id, func.coalesce(...)).group_by()`.
> Benefits: type-safe references, database portability, refactoring support,
> better errors, IDE navigation.

```
     142:                 preset=agent.preset,
     143:             )
     144:
>>>  145:     async def list_agents_last_activity(self) -> dict[AgentID, datetime | None]:
>>>  146:         """Return a mapping of agent_id -> last activity timestamp (UTC) or None.
>>>  147:
>>>  148:         Activity considers any of: event event_at, run finished_at, run started_at, or
>>>  149:         agent created_at as a fallback, taking the maximum.
>>>  150:         """
>>>  151:         async with self._session() as session:
>>>  152:             # This is complex to do purely in ORM, so we'll use raw SQL
>>>  153:             result = await session.execute(
>>>  154:                 text("""
>>>  155: SELECT a.id as agent_id,
>>>  156:        MAX(
>>>  157:          COALESCE(e.event_at, r.finished_at, r.started_at, a.created_at)
>>>  158:        ) as last_ts
>>>  159: FROM agents a
>>>  160: LEFT JOIN runs r ON r.agent_id = a.id
>>>  161: LEFT JOIN events e ON e.run_id = r.id
>>>  162: GROUP BY a.id
>>>  163:                     """)
>>>  164:             )
>>>  165:             return {AgentID(row.agent_id): row.last_ts for row in result}
     166:
     167:     async def delete_agent(self, agent_id: AgentID) -> None:
     168:         """Delete an agent and all associated records (cascaded by ORM)."""
```

### `redundant-exit-handler.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/redundant-exit-handler.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/git_commit_ai/cli.py#L660-L734)

File: `adgn/src/adgn/git_commit_ai/cli.py` (L660, L733-734)

> cli.py async_main() (lines 659-734) has a try-except handler that catches
> ExitWithCode exceptions only to immediately call sys.exit() with the same code.
> This adds 4 lines and indents 70+ lines of main logic for no benefit.
>
> Problems: (1) redundant indentation of all main logic, (2) handler doesn't
> transform, log, or enrich the exit code, (3) misleading - suggests special
> handling that doesn't exist, (4) verbosity.
>
> Remove the try-except entirely. Let ExitWithCode propagate to the top level;
> Python's default behavior will still terminate with the exit code. Or if clean
> exit is needed, the existing sys.exit() calls at the end are sufficient.
>
> Benefits: 4 fewer lines, one less indent level, clearer code without false
> suggestion of special handling. Top-level functions typically don't catch their
> own exit exceptions.

```
     657:
     658:
     659: async def async_main(argv: list[str] | None = None):
>>>  660:     try:
     661:         start_monotonic_s = time.monotonic()
     662:         gitdir = pygit2.discover_repository(str(Path.cwd()))
     663:         if not gitdir:
   ...
     730:
     731:         code = await _run_editor_flow(repo, msg, previous_message, stats_comment, passthru)
     732:         sys.exit(code)
>>>  733:     except ExitWithCode as e:
>>>  734:         sys.exit(e.code)
     735:
     736:
     737: def main():
```

### `redundant-policy-error-enum.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/redundant-policy-error-enum.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/agent/models/policy_error.py#L9-L22)

File: `adgn/src/adgn/agent/models/policy_error.py` (L9-11, L14-17, L21-22)

> Lines 9-11 define `PolicyErrorCode` enum with `READ_ERROR` and `PARSE_ERROR` values. Lines 14-17
> define `PolicyErrorStage` enum with `READ`, `PARSE`, and `TESTS` values. Lines 21-22 in `PolicyError`
> model include both `stage: PolicyErrorStage` and `code: PolicyErrorCode` fields.
>
> These enums are redundant: error code is always stage + "\_error" suffix. Having both requires
> keeping enums in sync when adding stages, creates confusing dual representation, and leaves TESTS
> stage without corresponding error code. PolicyError fields are redundant (code fully determined by stage).
>
> Keep only `PolicyErrorStage` enum. Remove `code` field from `PolicyError` model (lines 21-22) or
> add `@property def code()` that returns `f"{self.stage}_error"` for backwards compatibility. Alternatively,
> merge into single unified enum with `READ_ERROR`, `PARSE_ERROR`, `TESTS_ERROR` values. Eliminates
> duplication, easier maintenance, no mismatch risk, complete coverage.

```
       6: from pydantic import BaseModel, ConfigDict, Field
       7:
       8:
>>>    9: class PolicyErrorCode(StrEnum):
>>>   10:     READ_ERROR = "read_error"
>>>   11:     PARSE_ERROR = "parse_error"
      12:
      13:
>>>   14: class PolicyErrorStage(StrEnum):
>>>   15:     READ = "read"
>>>   16:     PARSE = "parse"
>>>   17:     TESTS = "tests"
      18:
      19:
      20: class PolicyError(BaseModel):
>>>   21:     stage: PolicyErrorStage = Field(description="Processing stage where error occurred")
>>>   22:     code: PolicyErrorCode = Field(description="Error code (read_error, parse_error)")
      23:     index: int | None = Field(None, description="Character/token index where error occurred")
      24:     length: int | None = Field(None, description="Length of error span in characters/tokens")
      25:     message: str | None = Field(None, description="Human-readable error message")
```

### `redundant-runtime-type-check.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/redundant-runtime-type-check.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/agent/policy_eval/container.py#L34-L35)

File: `adgn/src/adgn/agent/policy_eval/container.py` (L34-35)

> Redundant runtime type check for parameter when type system already guarantees non-None.
>
> **Current code (container.py:34-35):**
>
> ```python
> def __init__(self, agent_id: AgentID, ...):
>     if not agent_id:
>         raise ValueError("ContainerPolicyEvaluator requires agent_id")
> ```
>
> The type annotation `agent_id: AgentID` (not `AgentID | None`) already guarantees
> the parameter is provided. This check adds defensive programming noise without value.
>
> **The correct approach:**
>
> Remove the check. The type system guarantees `agent_id` is present. If you need
> to validate empty strings, add validation to the `AgentID` type itself:
>
> ```python
> class AgentID(str):
>     def __new__(cls, value: str):
>         if not value:
>             raise ValueError("AgentID cannot be empty")
>         return super().__new__(cls, value)
> ```
>
> This centralizes validation at the type level, not at every usage site.

```
      31:         engine: ApprovalPolicyEngine,
      32:         image: str | None = None,
      33:         timeout_secs: float | None = None,
>>>   34:     ) -> None:
>>>   35:         if not agent_id:
      36:             raise ValueError("ContainerPolicyEvaluator requires agent_id")
      37:         self.agent_id = agent_id
      38:         self.image: str = image or resolve_runtime_image()
```

### `runtime-lifecycle-confusion.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/runtime-lifecycle-confusion.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/agent/runtime/local_runtime.py#L81-L165)

File: `adgn/src/adgn/agent/runtime/local_runtime.py` (L81-82, L85-88, L90-153, L155-158, L160-165)

> `LocalAgentRuntime` has lifecycle issues: missing type annotations
> (ui_bus, connection_manager at 81-82), "may be initialized" antipattern
> (session/agent nullable at 85-88, runtime checks at 155-158), incomplete
> cleanup (close() doesn't null fields at 160-165), and not being a proper
> context manager despite having start()/close() methods.
>
> "May be initialized" antipattern impact: object exists but isn't usable
> (half-initialized), every method must check initialization, type system
> can't help (fields are `T | None`), easy to forget start() call.
>
> Solutions: (1) async context manager (move start() logic to **aenter**,
> cleanup to **aexit**, automatic lifecycle, strong types, guaranteed
> cleanup), or (2) factory pattern (classmethod create() with async init,
> manual lifecycle but strong types).
>
> Current approach: manual unclear lifecycle, weak type safety, incomplete
> cleanup.

```
      78:         self.model = model
      79:         self._client_factory = client_factory
      80:         self._system_override = system_override
>>>   81:         self._reasoning_effort = reasoning_effort
>>>   82:         self._reasoning_summary = reasoning_summary
      83:         self._parallel_tool_calls = parallel_tool_calls
      84:         self._extra_handlers = list(extra_handlers)
>>>   85:         self._ui_bus = ui_bus
>>>   86:         self._connection_manager = connection_manager
>>>   87:
>>>   88:         # Initialized by start()
      89:         self.session: AgentSession | None = None
>>>   90:         self.agent: MiniCodex | None = None
>>>   91:
>>>   92:     async def start(self) -> None:
>>>   93:         # Create session with UI components if provided
>>>   94:         sess = AgentSession(
>>>   95:             manager=self._connection_manager,
>>>   96:             approval_hub=self.running.approval_hub,
>>>   97:             persistence=self.running.approval_engine.persistence,
>>>   98:             agent_id=self.running.agent_id,
>>>   99:             ui_bus=self._ui_bus,
>>>  100:             approval_engine=self.running.approval_engine,
>>>  101:         )
>>>  102:
>>>  103:         # LLM client
>>>  104:         client = self._client_factory(self.model)
>>>  105:
>>>  106:         # Define run ID helper
>>>  107:         def _get_run_id():
>>>  108:             return sess.active_run.run_id if sess.active_run else None
>>>  109:
>>>  110:         # Build handlers
>>>  111:         handlers, persist_handler = build_handlers(
>>>  112:             poll_notifications=self.running.notifications_buffer.poll,
>>>  113:             manager=self._connection_manager,
>>>  114:             persistence=self.running.approval_engine.persistence,
>>>  115:             approval_engine=self.running.approval_engine,
>>>  116:             approval_hub=self.running.approval_hub,
>>>  117:             get_run_id=_get_run_id,
>>>  118:             agent_id=self.running.agent_id,
>>>  119:             ui_bus=self._ui_bus,
>>>  120:         )
>>>  121:
>>>  122:         # Set persist handler on session
>>>  123:         sess.set_persist_handler(persist_handler)
>>>  124:
>>>  125:         # Compose base system text and dynamic instruction provider
>>>  126:         base_system = self._system_override or str(get_ui_system_message())
>>>  127:
>>>  128:         async def _dynamic_instructions() -> str:
>>>  129:             """Dynamically generate instructions from compositor state."""
>>>  130:             meta = CompositorMetaClient(self.running.compositor_client)
>>>  131:             states = await meta.list_states()
>>>  132:             text: str = render_compositor_instructions(states)
>>>  133:             return text
>>>  134:
>>>  135:         # Create agent
>>>  136:         agent = await MiniCodex.create(
>>>  137:             model=self.model,
>>>  138:             mcp_client=self.running.compositor_client,
>>>  139:             system=base_system,
>>>  140:             client=client,
>>>  141:             handlers=list(handlers) + self._extra_handlers,
>>>  142:             dynamic_instructions=_dynamic_instructions,
>>>  143:             reasoning_effort=self._reasoning_effort,
>>>  144:             reasoning_summary=self._reasoning_summary,
>>>  145:             parallel_tool_calls=self._parallel_tool_calls,
>>>  146:         )
>>>  147:
>>>  148:         # Store system used for persisted run metadata
>>>  149:         sess.attach_agent(agent, model=self.model, system=base_system)
>>>  150:
>>>  151:         # Store references
>>>  152:         self.session = sess
>>>  153:         self.agent = agent
     154:
>>>  155:     async def run(self, user_text: str) -> AgentResult:
>>>  156:         """Raises RuntimeError if agent not started."""
>>>  157:         if self.agent is None:
>>>  158:             raise RuntimeError("agent not started - call start() first")
     159:
>>>  160:         return await self.agent.run(user_text)
>>>  161:
>>>  162:     async def close(self) -> None:
>>>  163:         """Does NOT close the underlying RunningInfrastructure.
>>>  164:         Call running.close() separately if needed.
>>>  165:         """
     166:         if self.session is not None:
     167:             await self.session.cancel_active_run()
```

### `ui-factories-helpers-missing.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/issues/ui-factories-helpers-missing.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-00/code/adgn/src/adgn/agent/web/src/components/GlobalApprovalsList.svelte#L69-L180)

File: `adgn/src/adgn/agent/web/src/components/GlobalApprovalsList.svelte` (L69-71, L78, L107, L115-121, L138-142, L175-180)

> GlobalApprovalsList.svelte contains explicit tool/resource constructions at 6 locations instead
> of using factories/helpers with defaults: MCP client creation (line 69: createMCPClient with
> name/url/token), resource subscription (line 78: subscribeToResource with URI), resource reading
> (line 107: readResource with URI), approval parsing (lines 115-121: manual object construction
> with agent_id/tool_call/timestamp), approve tool call (lines 138-142: callTool with approve_tool_call
> and agent_id/call_id), reject tool call (lines 175-180: callTool with reject_tool_call and
> agent_id/call_id/reason).
>
> This creates verbose boilerplate (repeated patterns), no default values (must specify all parameters),
> hard to test (can't mock without recreating full objects), duplication (same patterns across component),
> and fragile (API changes require updating many call sites).
>
> Create factories/helpers: `createApprovalsClient(options?)` with default name/url/token,
> `fetchPendingApprovals(client)`, `approveToolCall(client, agentId, callId)`,
> `parseApprovalContents(contents)`. Provides default values, centralized logic, easier testing
> (mock helpers not raw calls), type safety, less duplication.

```
      66:
      67:       // Connect to MCP server (requires backend to expose MCP endpoint)
      68:       // In a full implementation, this would connect to something like:
>>>   69:       // http://localhost:8765/api/mcp
>>>   70:       mcpClient = await createMCPClient({
>>>   71:         name: 'global-approvals-ui',
      72:         url: `${window.location.origin}/api/mcp`,
      73:         token
      74:       })
      75:
      76:       // Subscribe to resource updates for live refresh
      77:       // NOTE: Subscription support would need to be added to the backend
>>>   78:       try {
      79:         await subscribeToResource(mcpClient, MCPUris.approvalsPendingUri)
      80:       } catch (e) {
      81:         console.warn('Subscription not supported, will use polling:', e)
   ...
     104:    * Fetch all pending approvals from the global mailbox
     105:    *
     106:    * The resource://approvals/pending resource returns multiple TextResourceContents blocks,
>>>  107:    * where each block contains a JSON-serialized approval.
     108:    */
     109:   async function fetchApprovals() {
     110:     if (!mcpClient) return
   ...
     112:     try {
     113:       // Read the global approvals resource
     114:       const contents = await readResource(mcpClient, MCPUris.approvalsPendingUri)
>>>  115:
>>>  116:       // Parse contents - it returns an array of TextResourceContents
>>>  117:       // Each block has: { uri, mimeType, text }
>>>  118:       // The text field contains JSON with: { agent_id, tool_call: { name, call_id, args_json }, timestamp }
>>>  119:       const parsedApprovals: Array<PendingApproval & { agent_id: string }> = []
>>>  120:
>>>  121:       for (const block of contents) {
     122:         if ('text' in block && block.mimeType === 'application/json') {
     123:           try {
     124:             const data = JSON.parse(block.text)
   ...
     135:
     136:       approvals = parsedApprovals
     137:       error = null
>>>  138:
>>>  139:     } catch (e) {
>>>  140:       error = `Failed to fetch approvals: ${e instanceof Error ? e.message : String(e)}`
>>>  141:       console.error('Fetch error:', e)
>>>  142:     }
     143:   }
     144:
     145:   /**
   ...
     172:    */
     173:   function showRejectDialogFor(agentId: string, callId: string) {
     174:     rejectAgentId = agentId
>>>  175:     rejectCallId = callId
>>>  176:     rejectReason = ''
>>>  177:     showRejectDialog = true
>>>  178:   }
>>>  179:
>>>  180:   /**
     181:    * Reject a tool call via MCP tool with reason
     182:    */
     183:   async function handleReject() {
```

## crush/2025-08-30-internal_db (14)

### `config-nil-chains.yaml` / `occ-0` [P20]

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/crush/2025-08-30-internal_db/issues/config-nil-chains.yaml) · [code](https://github.com/agentydragon/crush/blob/a2a1ffa00943aa373f688ac05b667083ac3230b1/internal/diff/external.go#L41-L93)

File: `internal/diff/external.go` (L41-43, L92-93)

> Call-sites frequently chain nil checks (cfg != nil && cfg.Options != nil && cfg.Options.X != nil ...) which is
> noisy and error-prone. Centralize nil-safe accessors on Config (nil-receiver-safe methods) or pass
> \*config.Config by DI to eliminate repetitive pointer chains and consolidate defaults.
>
> **Note:** Example: Diff.ExternalCommand / ParseMode guarded by multi-level nil checks; centralize via
> config.Diff().ParseMode or a nil-safe helper.

### `config-nil-chains.yaml` / `occ-1` [P20]

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/crush/2025-08-30-internal_db/issues/config-nil-chains.yaml) · [code](https://github.com/agentydragon/crush/blob/a2a1ffa00943aa373f688ac05b667083ac3230b1/internal/lsp/watcher/watcher.go#L320-L356)

File: `internal/lsp/watcher/watcher.go` (L320-356)

> Call-sites frequently chain nil checks (cfg != nil && cfg.Options != nil && cfg.Options.X != nil ...) which is
> noisy and error-prone. Centralize nil-safe accessors on Config (nil-receiver-safe methods) or pass
> \*config.Config by DI to eliminate repetitive pointer chains and consolidate defaults.
>
> **Note:** Numerous checks for cfg.Options.DebugLSP and config-derived guards; prefer
> config.DebugLSP()/config.CurrentLSPIgnore() helpers or DI.

### `config-nil-chains.yaml` / `occ-2` [P20]

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/crush/2025-08-30-internal_db/issues/config-nil-chains.yaml) · [code](https://github.com/agentydragon/crush/blob/a2a1ffa00943aa373f688ac05b667083ac3230b1/internal/llm/tools/tools.go#L1-L30)

File: `internal/llm/tools/tools.go` (L1-30)

> Call-sites frequently chain nil checks (cfg != nil && cfg.Options != nil && cfg.Options.X != nil ...) which is
> noisy and error-prone. Centralize nil-safe accessors on Config (nil-receiver-safe methods) or pass
> \*config.Config by DI to eliminate repetitive pointer chains and consolidate defaults.
>
> **Note:** Representative site for reading GrepTimeoutSecs, BashBlockedCommands, MaxToolOutputBytes — prefer
> config.GrepTimeoutSecs(), config.BashBlockedCommands() helpers or DI.

### `hardcoded-timeouts.yaml` / `occ-0` [P20]

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/crush/2025-08-30-internal_db/issues/hardcoded-timeouts.yaml) · [code](https://github.com/agentydragon/crush/blob/a2a1ffa00943aa373f688ac05b667083ac3230b1/internal/lsp/client.go#L241-L526)

File: `internal/lsp/client.go` (L241-246, L312-318, L316-319, L522-526)

> Hardcoded timeouts, intervals, and numeric limits are scattered across subsystems (LSP client, diff runner,
> app, watcher, agent, sourcegraph). Name these values and centralize them (either as package-level consts or
> configurable options) to make tuning, consistency, and discovery easier. Where appropriate, consider making
> them configuration options (with safe defaults). Preserve local comments about semantics when migrating to
> named constants.
>
> **Note:** LSP client: Close() uses 5*time.Second; WaitForServerReady uses 30*time.Second and ticker
> 500\*time.Millisecond; maxFilesToOpen constant-like value 5. Define named consts like LSPStopTimeout,
> LSPWaitReadyTimeout, LSPReadyPollInterval, MaxFilesToOpen.

### `hardcoded-timeouts.yaml` / `occ-1` [P20]

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/crush/2025-08-30-internal_db/issues/hardcoded-timeouts.yaml) · [code](https://github.com/agentydragon/crush/blob/a2a1ffa00943aa373f688ac05b667083ac3230b1/internal/diff/external.go#L54-L63)

File: `internal/diff/external.go` (L54-63)

> Hardcoded timeouts, intervals, and numeric limits are scattered across subsystems (LSP client, diff runner,
> app, watcher, agent, sourcegraph). Name these values and centralize them (either as package-level consts or
> configurable options) to make tuning, consistency, and discovery easier. Where appropriate, consider making
> them configuration options (with safe defaults). Preserve local comments about semantics when migrating to
> named constants.
>
> **Note:** External diff runner uses context.WithTimeout(..., 2*time.Second). Define ExternalDiffTimeout = 2 *
> time.Second or make configurable via config.Diff.ExternalCommand timeout.

### `hardcoded-timeouts.yaml` / `occ-2` [P20]

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/crush/2025-08-30-internal_db/issues/hardcoded-timeouts.yaml) · [code](https://github.com/agentydragon/crush/blob/a2a1ffa00943aa373f688ac05b667083ac3230b1/internal/lsp/diagnostics_wait.go#L24-L42)

File: `internal/lsp/diagnostics_wait.go` (L24-42)

> Hardcoded timeouts, intervals, and numeric limits are scattered across subsystems (LSP client, diff runner,
> app, watcher, agent, sourcegraph). Name these values and centralize them (either as package-level consts or
> configurable options) to make tuning, consistency, and discovery easier. Where appropriate, consider making
> them configuration options (with safe defaults). Preserve local comments about semantics when migrating to
> named constants.
>
> **Note:** Diagnostics wait loop uses 5s deadline and 100ms poll interval; name these constants (DiagnosticsWaitTimeout /
> DiagnosticsPollInterval).

### `hardcoded-timeouts.yaml` / `occ-3` [P20]

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/crush/2025-08-30-internal_db/issues/hardcoded-timeouts.yaml) · [code](https://github.com/agentydragon/crush/blob/a2a1ffa00943aa373f688ac05b667083ac3230b1/internal/app/lsp.go#L40-L122)

File: `internal/app/lsp.go` (L40-46, L118-122)

> Hardcoded timeouts, intervals, and numeric limits are scattered across subsystems (LSP client, diff runner,
> app, watcher, agent, sourcegraph). Name these values and centralize them (either as package-level consts or
> configurable options) to make tuning, consistency, and discovery easier. Where appropriate, consider making
> them configuration options (with safe defaults). Preserve local comments about semantics when migrating to
> named constants.
>
> **Note:** App LSP init uses 30s init timeout; shutdown uses 5s shutdown timeout; name them LSPInitTimeout,
> LSPShutdownTimeout.

### `hardcoded-timeouts.yaml` / `occ-4` [P20]

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/crush/2025-08-30-internal_db/issues/hardcoded-timeouts.yaml) · [code](https://github.com/agentydragon/crush/blob/a2a1ffa00943aa373f688ac05b667083ac3230b1/internal/app/app.go#L76-L312)

File: `internal/app/app.go` (L76-81, L304-310, L306-312)

> Hardcoded timeouts, intervals, and numeric limits are scattered across subsystems (LSP client, diff runner,
> app, watcher, agent, sourcegraph). Name these values and centralize them (either as package-level consts or
> configurable options) to make tuning, consistency, and discovery easier. Where appropriate, consider making
> them configuration options (with safe defaults). Preserve local comments about semantics when migrating to
> named constants.
>
> **Note:** App-wide timers: middleware debounce 30ms; select/drop timeout 2s; slow-op threshold (100ms) and shutdown
> timeout (5s) should be named constants or config options.

### `hardcoded-timeouts.yaml` / `occ-5` [P20]

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/crush/2025-08-30-internal_db/issues/hardcoded-timeouts.yaml) · [code](https://github.com/agentydragon/crush/blob/a2a1ffa00943aa373f688ac05b667083ac3230b1/internal/lsp/watcher/watcher.go#L64-L92)

File: `internal/lsp/watcher/watcher.go` (L64-72, L74-80, L86-92)

> Hardcoded timeouts, intervals, and numeric limits are scattered across subsystems (LSP client, diff runner,
> app, watcher, agent, sourcegraph). Name these values and centralize them (either as package-level consts or
> configurable options) to make tuning, consistency, and discovery easier. Where appropriate, consider making
> them configuration options (with safe defaults). Preserve local comments about semantics when migrating to
> named constants.
>
> **Note:** Watcher defaults: debounceTime 300ms, default recursive max watched dirs 5000, default watch mode "recursive"
> — name these watcher defaults.

### `hardcoded-timeouts.yaml` / `occ-6` [P20]

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/crush/2025-08-30-internal_db/issues/hardcoded-timeouts.yaml) · [code](https://github.com/agentydragon/crush/blob/a2a1ffa00943aa373f688ac05b667083ac3230b1/internal/llm/agent/sequence_transformer.go#L1-L199)

File: `internal/llm/agent/sequence_transformer.go` (L1-199)

> Hardcoded timeouts, intervals, and numeric limits are scattered across subsystems (LSP client, diff runner,
> app, watcher, agent, sourcegraph). Name these values and centralize them (either as package-level consts or
> configurable options) to make tuning, consistency, and discovery easier. Where appropriate, consider making
> them configuration options (with safe defaults). Preserve local comments about semantics when migrating to
> named constants.
>
> **Note:** Sequence transformer timing: overall deadline 1500ms; small sleep 50ms; per-call timeout 2500ms — name and
> centralize as AgentSequenceTimeouts.

### `hardcoded-timeouts.yaml` / `occ-7` [P20]

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/crush/2025-08-30-internal_db/issues/hardcoded-timeouts.yaml) · [code](https://github.com/agentydragon/crush/blob/a2a1ffa00943aa373f688ac05b667083ac3230b1/internal/llm/agent/agent.go#L1-L240)

File: `internal/llm/agent/agent.go` (L1-240)

> Hardcoded timeouts, intervals, and numeric limits are scattered across subsystems (LSP client, diff runner,
> app, watcher, agent, sourcegraph). Name these values and centralize them (either as package-level consts or
> configurable options) to make tuning, consistency, and discovery easier. Where appropriate, consider making
> them configuration options (with safe defaults). Preserve local comments about semantics when migrating to
> named constants.
>
> **Note:** Agent: 50ms delayed flush, 5s overall timeout, 200ms retry sleep — name these and consider DI/config.

### `hardcoded-timeouts.yaml` / `occ-8` [P20]

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/crush/2025-08-30-internal_db/issues/hardcoded-timeouts.yaml) · [code](https://github.com/agentydragon/crush/blob/a2a1ffa00943aa373f688ac05b667083ac3230b1/internal/llm/tools/sourcegraph.go#L1-L240)

File: `internal/llm/tools/sourcegraph.go` (L1-240)

> Hardcoded timeouts, intervals, and numeric limits are scattered across subsystems (LSP client, diff runner,
> app, watcher, agent, sourcegraph). Name these values and centralize them (either as package-level consts or
> configurable options) to make tuning, consistency, and discovery easier. Where appropriate, consider making
> them configuration options (with safe defaults). Preserve local comments about semantics when migrating to
> named constants.
>
> **Note:** Sourcegraph HTTP client timeouts: Timeout 30s, IdleConnTimeout 90s — name them and centralize.

### `path-schema-docs-mismatch.yaml` / `occ-0` [P20]

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/crush/2025-08-30-internal_db/issues/path-schema-docs-mismatch.yaml) · [code](https://github.com/agentydragon/crush/blob/a2a1ffa00943aa373f688ac05b667083ac3230b1/internal/llm/tools/ls.go#L109-L123)

File: `internal/llm/tools/ls.go` (L109, L119-123)

> Path schema/docs are inconsistent with runtime behavior in internal/llm/tools; the spec (schema/docs) and
> implementation
> disagree. Resolve by aligning the declared contract with code or updating the code to meet the declared
> contract.
>
> **Note:** ToolInfo.Required lists "path" as required (line 109), but Run allows empty path and defaults to workingDir
> (lines 119-123).

### `path-schema-docs-mismatch.yaml` / `occ-1` [P20]

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/crush/2025-08-30-internal_db/issues/path-schema-docs-mismatch.yaml) · [code](https://github.com/agentydragon/crush/blob/a2a1ffa00943aa373f688ac05b667083ac3230b1/internal/llm/tools/edit.go#L48-L157)

File: `internal/llm/tools/edit.go` (L48-104, L155-157)

> Path schema/docs are inconsistent with runtime behavior in internal/llm/tools; the spec (schema/docs) and
> implementation
> disagree. Resolve by aligning the declared contract with code or updating the code to meet the declared
> contract.
>
> **Note:** Description says absolute path only, but Run joins relative paths with workingDir.

## ducktape/2025-11-20-01 (12)

### `admin-server-fixture-needed.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/issues/admin-server-fixture-needed.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/code/adgn/tests/agent/test_policy_validation_reload.py#L43-L146)

File: `adgn/tests/agent/test_policy_validation_reload.py` (L43, L56, L70, L90, L107, L122, L133, L146)

> Repeated admin_server creation should be a shared fixture.
>
> Every test creates its own `ApprovalPolicyAdminServer(engine=engine)`. This appears at lines 43, 56, 70, 90,
> 107, 122,
> 133, 146.
>
> Should be a fixture that depends on the `engine` fixture.
>
> Benefits:
>
> - DRY principle
> - Consistent setup across tests
> - Easy to modify server configuration

```
      40:     """Test validating a valid policy."""
      41:     engine, _ = engine_and_persistence
      42:
>>>   43:     admin_server = ApprovalPolicyAdminServer(engine=engine)
      44:
      45:     # Valid Python code
      46:     result = await admin_server._mcp_server._tools["validate_policy"].fn(ValidatePolicyArgs(source="print('hello')"))
   ...
      53:     """Test validating a policy with syntax errors."""
      54:     engine, _ = engine_and_persistence
      55:
>>>   56:     admin_server = ApprovalPolicyAdminServer(engine=engine)
      57:
      58:     # Invalid syntax
      59:     result = await admin_server._mcp_server._tools["validate_policy"].fn(ValidatePolicyArgs(source="print('hello'"))
   ...
      67:     """Test validating a policy that fails at runtime."""
      68:     engine, _ = engine_and_persistence
      69:
>>>   70:     admin_server = ApprovalPolicyAdminServer(engine=engine)
      71:
      72:     # Syntactically valid but fails self-check (wrong structure)
      73:     result = await admin_server._mcp_server._tools["validate_policy"].fn(
   ...
      87:     new_policy = "print('from persistence')"
      88:     await persistence.set_policy(engine.agent_id, content=new_policy)
      89:
>>>   90:     admin_server = ApprovalPolicyAdminServer(engine=engine)
      91:
      92:     # Change engine's in-memory policy
      93:     engine.set_policy("print('different')")
   ...
     104:     """Test reloading policy from provided source."""
     105:     engine, _ = engine_and_persistence
     106:
>>>  107:     admin_server = ApprovalPolicyAdminServer(engine=engine)
     108:
     109:     # Reload with provided source
     110:     new_source = load_default_policy_source()
   ...
     119:     """Test that reload validates the source before setting."""
     120:     engine, _ = engine_and_persistence
     121:
>>>  122:     admin_server = ApprovalPolicyAdminServer(engine=engine)
     123:
     124:     # Try to reload with invalid source
     125:     with pytest.raises(Exception):  # Should fail validation
   ...
     130:     """Test that reloading from empty persistence raises error."""
     131:     engine, persistence = engine_and_persistence
     132:
>>>  133:     admin_server = ApprovalPolicyAdminServer(engine=engine)
     134:
     135:     # Create a new agent with no policy in persistence
     136:     new_agent_id = await persistence.create_agent(mcp_config=MCPConfig(), metadata=AgentMetadata(preset="test"))
   ...
     143:         policy_source=load_default_policy_source(),
     144:     )
     145:
>>>  146:     new_admin_server = ApprovalPolicyAdminServer(engine=new_engine)
     147:
     148:     # Try to reload (should fail - no policy in persistence)
     149:     with pytest.raises(ValueError, match="No policy found in persistence"):
```

### `complex-nested-loop-assertion.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/issues/complex-nested-loop-assertion.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/code/adgn/tests/agent/test_mcp_notifications_flow.py#L136-L147)

File: `adgn/tests/agent/test_mcp_notifications_flow.py` (L136-147)

> Lines 136-147: Complex 12-line assertion with nested loops, boolean flag,
> and break statements should be replaced with declarative hamcrest matcher.
>
> **What it checks:**
> captured[-1].input contains a UserMessage with content containing InputTextPart
> where text includes "<system notification>".
>
> **Current approach:** Imperative loops with mutable found flag, manual
> isinstance checks, nested breaks.
>
> **Should use:** Hamcrest matchers like has_properties, instance_of, and
> contains_string to express the same check declaratively.

```
     133:         await agent.run("go")
     134:
     135:         # The second create call (post-tool) should include the injected system notification
>>>  136:         assert_that(captured, has_length(greater_than_or_equal_to(2)), "expected at least two sampling calls")
>>>  137:         second = captured[-1]
>>>  138:         found = False
>>>  139:         for msg in second.input or []:
>>>  140:             if isinstance(msg, UserMessage):
>>>  141:                 for c in msg.content or []:
>>>  142:                     if isinstance(c, InputTextPart) and "<system notification>" in c.text:
>>>  143:                         found = True
>>>  144:                         break
>>>  145:             if found:
>>>  146:                 break
>>>  147:         assert found, "expected system notification after tool-triggered update"
     148:
     149:
     150: async def test_notifications_broadcast_outside_tool(responses_factory: ResponsesFactory, make_buffered_client):
```

### `delete-coerce-error-data.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/issues/delete-coerce-error-data.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/code/adgn/src/adgn/mcp/policy_gateway/signals.py#L56-L122)

File: `adgn/src/adgn/mcp/policy_gateway/signals.py` (L56-60, L62-93, L116, L119, L122)

> Lines 62-93 define \_coerce_error_data that tries to coerce various error representations
> to mtypes.ErrorData with extensive defensive fallbacks. Lines 56-60 define a Protocol
> for attribute-based fallback. This overly defensive function should be deleted entirely.
>
> Problems: swallows validation errors and tries manual construction (lines 75-85), has
> attribute-based fallback for objects with .code/.message (lines 87-92), mixes validation
> with data extraction, violates fail-fast principle, makes debugging harder.
>
> Delete \_coerce_error_data and \_ErrorFields Protocol. Replace three usage sites (lines
> 116, 119, 122) with direct mtypes.ErrorData.model_validate() calls. If data doesn't
> match schema, Pydantic raises clear validation errors instead of silently constructing
> minimal ErrorData or returning None.

```
      53: _MSG_TO_KIND: dict[str, PolicyGatewayErrorKind] = {msg: kind for _code, msg, kind in _KINDS}
      54:
      55:
>>>   56: @runtime_checkable
>>>   57: class _ErrorFields(Protocol):
>>>   58:     code: Any
>>>   59:     message: Any
>>>   60:
      61:
>>>   62: def _coerce_error_data(obj: Any) -> mtypes.ErrorData | None:
>>>   63:     """Attempt to coerce various error representations to mcp.types.ErrorData.
>>>   64:
>>>   65:     - Accepts dicts, already-typed ErrorData, or objects with .code/.message attributes.
>>>   66:     - Returns None if no minimally-typed shape is available.
>>>   67:     """
>>>   68:     if isinstance(obj, mtypes.ErrorData):
>>>   69:         return obj
>>>   70:     if isinstance(obj, dict):
>>>   71:         try:
>>>   72:             return mtypes.ErrorData.model_validate(obj)
>>>   73:         except Exception as e:
>>>   74:             logger.debug("Failed to validate dict as ErrorData: %s", e)
>>>   75:             try:
>>>   76:                 # Minimal acceptance: just code+message fields
>>>   77:                 code_val = obj.get("code")
>>>   78:                 msg_val = obj.get("message")
>>>   79:                 if code_val is None or msg_val is None:
>>>   80:                     logger.debug("Dict missing code or message fields")
>>>   81:                     return None
>>>   82:                 return mtypes.ErrorData(code=int(code_val), message=str(msg_val))
>>>   83:             except Exception as e2:
>>>   84:                 logger.debug("Failed to construct minimal ErrorData from dict: %s", e2)
>>>   85:                 return None
>>>   86:     # Attribute-style fallback
>>>   87:     if isinstance(obj, _ErrorFields):
>>>   88:         try:
>>>   89:             return mtypes.ErrorData(code=int(obj.code), message=str(obj.message))
>>>   90:         except Exception as e:
>>>   91:             logger.debug("Failed to extract ErrorData from object attributes: %s", e)
>>>   92:             return None
>>>   93:     return None
      94:
      95:
      96: def detect_policy_gateway_error(
   ...
     113:     error_data: mtypes.ErrorData | None = None
     114:     # Check for CallToolResult with is_error=True
     115:     if (isinstance(err, FastMcpCallToolResult | mtypes.CallToolResult) and err.is_error) or isinstance(err, McpError):
>>>  116:         error_data = _coerce_error_data(err.error)
     117:     # Check for direct error data
     118:     elif isinstance(err, dict | mtypes.ErrorData):
>>>  119:         error_data = _coerce_error_data(err)
     120:     # Fallback: other exceptions with .error attribute
     121:     elif hasattr(err, "error"):
>>>  122:         error_data = _coerce_error_data(err.error)
     123:
     124:     # Map structured error first
     125:     if error_data is not None:
```

### `fragmented-assertions.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/issues/fragmented-assertions.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/code/adgn/tests/agent/test_runtime_timeout.py#L38-L40)

File: `adgn/tests/agent/test_runtime_timeout.py` (L38-40)

> Tests use multiple separate assertions instead of structured matchers (hamcrest or Pydantic model equality).
>
> Benefits of structured matchers:
>
> - Single assertion with clear expected structure
> - Better error messages showing which specific property failed or full diff
> - Less verbose code
> - More explicit about intent
>
> **Note:** Multiple separate assertions for object properties (instance type, exit_code, stdout); should use
> has_properties

```
      35:
      36:         # Next call should work; container should have been restarted
      37:         res_ok = await stub(ExecInput(cmd=["/bin/echo", "-n", "ok"], timeout_ms=5000, shell=False))
>>>   38:         assert_that(res_ok.exit, instance_of(Exited))
>>>   39:         assert res_ok.exit.exit_code == 0
>>>   40:         assert (res_ok.stdout or "") == "ok"
```

### `fragmented-assertions.yaml` / `occ-1`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/issues/fragmented-assertions.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/code/adgn/tests/agent/test_policy_validation_reload.py#L62-L79)

File: `adgn/tests/agent/test_policy_validation_reload.py` (L62-63, L77-79)

> Tests use multiple separate assertions instead of structured matchers (hamcrest or Pydantic model equality).
>
> Benefits of structured matchers:
>
> - Single assertion with clear expected structure
> - Better error messages showing which specific property failed or full diff
> - Less verbose code
> - More explicit about intent
>
> **Note:** Multiple assertions to check error messages (length > 0, then substring); should use
> has_item(contains_string(...))

```
      59:     result = await admin_server._mcp_server._tools["validate_policy"].fn(ValidatePolicyArgs(source="print('hello'"))
      60:
      61:     assert result.valid is False
>>>   62:     assert_that(result.errors, has_length(greater_than(0)))
>>>   63:     assert "Syntax error" in result.errors[0]
      64:
      65:
      66: async def test_validate_policy_runtime_error(engine_and_persistence, docker_client: DockerClient):
   ...
      74:         ValidatePolicyArgs(source="import sys; sys.exit(1)")
      75:     )
      76:
>>>   77:     assert result.valid is False
>>>   78:     assert_that(result.errors, has_length(greater_than(0)))
>>>   79:     assert "Runtime validation failed" in result.errors[0]
      80:
      81:
      82: async def test_reload_policy_from_persistence(engine_and_persistence, docker_client: DockerClient):
```

### `fragmented-assertions.yaml` / `occ-2`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/issues/fragmented-assertions.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/code/adgn/tests/mcp/approval_policy/test_policy_resources.py#L171-L321)

File: `adgn/tests/mcp/approval_policy/test_policy_resources.py` (L171-176, L213-218, L249-252, L289-290, L308-309, L320-321)

> Tests use multiple separate assertions instead of structured matchers (hamcrest or Pydantic model equality).
>
> Benefits of structured matchers:
>
> - Single assertion with clear expected structure
> - Better error messages showing which specific property failed or full diff
> - Less verbose code
> - More explicit about intent
>
> **Note:** Individual field assertions instead of structured comparison; should use Pydantic model equality or
> has_properties

```
     168:         assert result.isError is False
     169:
     170:         # Verify it was created in persistence
>>>  171:         policy = await persistence.get_policy("new-policy")
>>>  172:         assert policy is not None
>>>  173:         assert policy.id == "new-policy"
>>>  174:         assert policy.text == "print('new policy')"
>>>  175:         assert policy.description == "A new policy"
>>>  176:         assert policy.enabled is True
     177:
     178:     async def test_create_duplicate(self, admin_server, persistence):
     179:         """Test creating a policy with duplicate ID fails."""
   ...
     210:
     211:         assert result.isError is False
     212:
>>>  213:         policy = await persistence.get_policy("minimal")
>>>  214:         assert policy is not None
>>>  215:         assert policy.id == "minimal"
>>>  216:         assert policy.text == "pass"
>>>  217:         assert policy.description is None
>>>  218:         assert policy.enabled is True  # default
     219:
     220:
     221: class TestUpdatePolicyTool:
   ...
     246:         assert result.isError is False
     247:
     248:         # Verify the update
>>>  249:         policy = await persistence.get_policy("update-me")
>>>  250:         assert policy is not None
>>>  251:         assert policy.text == "print('v2')"
>>>  252:         assert policy.description == "Version 2"
     253:
     254:     async def test_update_nonexistent(self, admin_server):
     255:         """Test updating a nonexistent policy fails."""
   ...
     286:
     287:         # Check that history was created (requires accessing policy_history table)
     288:         # For now, just verify the update worked
>>>  289:         policy = await persistence.get_policy("versioned")
>>>  290:         assert policy.text == "print('v2')"
     291:
     292:
     293: class TestDeletePolicyTool:
   ...
     305:         )
     306:
     307:         # Verify it exists
>>>  308:         policy = await persistence.get_policy("delete-me")
>>>  309:         assert policy is not None
     310:
     311:         # Delete it
     312:         result = await admin_server._mcp_server.call_tool(
   ...
     317:         assert result.isError is False
     318:
     319:         # Verify it's gone
>>>  320:         policy = await persistence.get_policy("delete-me")
>>>  321:         assert policy is None
     322:
     323:     async def test_delete_nonexistent(self, admin_server):
     324:         """Test deleting a nonexistent policy succeeds (idempotent)."""
```

### `from-server-too-long.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/issues/from-server-too-long.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/code/adgn/src/adgn/mcp/stubs/typed_stubs.py#L109-L178)

File: `adgn/src/adgn/mcp/stubs/typed_stubs.py` (L109-178, L128-177)

> The from_server classmethod spans 69 lines (109-178), with a single for-loop body
> consuming 49 lines (128-177). This makes the method difficult to understand and maintain.
>
> Problems: single method doing too many things (registry access, tool introspection,
> type resolution, model extraction), 49-line loop body extremely hard to read, multiple
> nested try/except blocks and conditionals within loop, mixing different concerns,
> hard to test individual introspection logic pieces.
>
> Extract loop body into a static helper method \_extract_tool_models(tool) that returns
> tuple[str, ToolModels] | None. Simplify main loop to call helper, check result, and
> store. Benefits: single responsibility per method, easier to understand flow, helper
> testable independently, reduced cognitive load.

```
     106:         return self._models
     107:
     108:     @classmethod
>>>  109:     def from_server(cls, server: FastMCP, session: Client, *, exclude_none: bool = True) -> TypedClient:
>>>  110:         """Create a TypedClient introspecting FastMCP's tool registry.
>>>  111:
>>>  112:         Requires a server created via FastMCP. Uses server._tool_manager.list_tools()
>>>  113:         and reads each tool.fn_metadata.arg_model/output_model.
>>>  114:         """
>>>  115:         # Access the internal tool manager and fetch local tools synchronously
>>>  116:         try:
>>>  117:             tm = server._tool_manager  # type: ignore[attr-defined]
>>>  118:         except AttributeError as exc:
>>>  119:             raise RuntimeError("Server does not expose _tool_manager") from exc
>>>  120:         # Prefer local tools; mounted tools aren't needed for typed tests here
>>>  121:         try:
>>>  122:             tools_by_name = tm._tools  # type: ignore[attr-defined]
>>>  123:         except AttributeError as exc:
>>>  124:             raise RuntimeError("Server tool manager does not expose _tools") from exc
>>>  125:         tools = list(tools_by_name.values())
>>>  126:
>>>  127:         client = cls(session, exclude_none=exclude_none)
>>>  128:         for t in tools:
>>>  129:             try:
>>>  130:                 fm = t.fn_metadata  # type: ignore[attr-defined]
>>>  131:             except AttributeError:
>>>  132:                 fm = None
>>>  133:             try:
>>>  134:                 fn = t.fn  # type: ignore[attr-defined]
>>>  135:             except AttributeError:
>>>  136:                 fn = None
>>>  137:             hinted_input = None
>>>  138:             hinted_output = None
>>>  139:             if fn is not None:
>>>  140:                 try:
>>>  141:                     hinted_input = fn._mcp_flat_input_model  # type: ignore[attr-defined]
>>>  142:                 except AttributeError:
>>>  143:                     hinted_input = None
>>>  144:                 try:
>>>  145:                     hinted_output = fn._mcp_flat_output_model  # type: ignore[attr-defined]
>>>  146:                 except AttributeError:
>>>  147:                     hinted_output = None
>>>  148:             if fm is None:
>>>  149:                 # Fall back to flat-model hints only
>>>  150:                 arg_model = hinted_input
>>>  151:                 out_model = hinted_output
>>>  152:                 if not (isinstance(arg_model, type) and issubclass(arg_model, BaseModel)):
>>>  153:                     continue
>>>  154:             else:
>>>  155:                 arg_model = fm.arg_model  # type: ignore[attr-defined]
>>>  156:                 out_model = fm.output_model  # type: ignore[attr-defined]
>>>  157:                 if out_model is None or arg_model is None:
>>>  158:                     continue
>>>  159:
>>>  160:             if isinstance(hinted_input, type) and issubclass(hinted_input, BaseModel):
>>>  161:                 input_type: type[BaseModel] | None = hinted_input
>>>  162:             elif isinstance(arg_model, type) and issubclass(arg_model, BaseModel):
>>>  163:                 input_type = arg_model
>>>  164:             else:
>>>  165:                 input_type = None
>>>  166:
>>>  167:             try:
>>>  168:                 tool_key = t.key  # type: ignore[attr-defined]
>>>  169:             except AttributeError:
>>>  170:                 try:
>>>  171:                     tool_key = t.name  # type: ignore[attr-defined]
>>>  172:                 except AttributeError:
>>>  173:                     tool_key = None
>>>  174:             if not isinstance(tool_key, str) or not tool_key:
>>>  175:                 continue
>>>  176:             output_type = _resolve_output_type(hinted_output, out_model)
>>>  177:             client._models[tool_key] = ToolModels(Input=input_type, Output=output_type, _arg_model=arg_model)
>>>  178:         return client
     179:
     180:     def error(self, name: str) -> Callable[[BaseModel], Awaitable[str]]:
     181:         models = self._models.get(name)
```

### `message-wrapper-discriminator.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/issues/message-wrapper-discriminator.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/code/adgn/src/adgn/openai_utils/model.py#L26-L182)

File: `adgn/src/adgn/openai_utils/model.py` (L26-33, L36-43, L46-53, L93, L172-182)

> model.py input message types (AssistantMessage, UserMessage, SystemMessage lines
> 26-53) embed the discriminator field (role) directly in the message class,
> mixing API-level concerns with content structure.
>
> Current inconsistency: input messages use "role" as discriminator, other input
> items use "type" (ReasoningItem, FunctionCallItem), output messages use "kind"
> (AssistantMessageOut line 172-182). This creates three different discriminator
> naming conventions.
>
> Separate message from discriminator using wrapper pattern: message class contains
> content only, wrapper class contains discriminator "kind" plus message. This
> matches the output pattern (AssistantMessageOut) and enables clearer type
> discrimination for union types (InputItem line 93).
>
> Benefits: Consistent discriminator naming, separates transport/API concerns from
> content structure, message content can evolve independently from serialization
> format.

```
      23:     model_config = ConfigDict(extra="allow")
      24:
      25:
>>>   26: class AssistantMessage(BaseModel):
>>>   27:     role: Literal["assistant"] = "assistant"
>>>   28:     content: list[InputTextPart] | None = None
>>>   29:     model_config = ConfigDict(extra="allow")
>>>   30:
>>>   31:     @classmethod
>>>   32:     def text(cls, text: str) -> Self:
>>>   33:         return cls(content=[InputTextPart(text=text)])
      34:
      35:
>>>   36: class UserMessage(BaseModel):
>>>   37:     role: Literal["user"] = "user"
>>>   38:     content: list[InputTextPart]
>>>   39:     model_config = ConfigDict(extra="allow")
>>>   40:
>>>   41:     @classmethod
>>>   42:     def text(cls, text: str) -> Self:
>>>   43:         return cls(content=[InputTextPart(text=text)])
      44:
      45:
>>>   46: class SystemMessage(BaseModel):
>>>   47:     role: Literal["system"] = "system"
>>>   48:     content: list[InputTextPart]
>>>   49:     model_config = ConfigDict(extra="allow")
>>>   50:
>>>   51:     @classmethod
>>>   52:     def text(cls, text: str) -> Self:
>>>   53:         return cls(content=[InputTextPart(text=text)])
      54:
      55:
      56: class ReasoningSummaryItem(BaseModel):
   ...
      90:     model_config = ConfigDict(extra="allow")
      91:
      92:
>>>   93: InputItem = AssistantMessage | UserMessage | SystemMessage | ReasoningItem | FunctionCallItem | FunctionCallOutputItem
      94:
      95:
      96: class ToolChoiceFunction(BaseModel):
   ...
     169:     model_config = ConfigDict(extra="allow")
     170:
     171:
>>>  172: class AssistantMessageOut(BaseModel):
>>>  173:     """Adapter-level assistant message output (text parts only for now).
>>>  174:
>>>  175:     Matches the SDK's message content shape we actually use: a list of text parts
>>>  176:     with optional annotations. This keeps a stable, Pydantic-validated shape
>>>  177:     for downstream use and can be extended if we support non-text parts later.
>>>  178:     """
>>>  179:
>>>  180:     kind: Literal["assistant_message"] = "assistant_message"
>>>  181:     parts: list[OutputText]
>>>  182:     model_config = ConfigDict(extra="allow")
     183:
     184:     @model_validator(mode="before")
     185:     @classmethod
```

### `proposal-id-int-not-str.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/issues/proposal-id-int-not-str.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/code/adgn/src/adgn/agent/approvals.py#L211-L258)

File: `adgn/src/adgn/agent/approvals.py` (L211-220, L217, L236, L253, L258)

> Lines 211-220 define notify_proposal_change with str signature, but all three callers
> (lines 236, 253, 258) have int proposal_id and must explicitly convert with str().
> This indicates wrong method signature.
>
> Problem: all callers (create_proposal line 239, approve_proposal line 239, reject_proposal
> line 255) have proposal_id as int in their signatures, persistence layer likely uses
> int, URI formatting at line 217 works fine with int (f-string converts automatically),
> unnecessary conversions add cognitive load.
>
> Change notify_proposal_change signature to accept int instead of str. Callers can then
> pass int directly without conversion. Benefits: eliminates unnecessary conversions,
> makes type consistency clear, aligns with persistence layer.
>
> Related to issue 022 about using wrong ID in create_proposal.

```
     208:         if cb:
     209:             cb(APPROVAL_POLICY_PROPOSALS_INDEX_URI)
     210:
>>>  211:     def notify_proposal_change(self, proposal_id: str) -> None:
>>>  212:         """Notify about a specific proposal change and the proposals index.
>>>  213:
>>>  214:         Convenience method that combines notifying about a specific proposal item
>>>  215:         and the proposals index list change.
>>>  216:         """
>>>  217:         self.notify_resource(f"{APPROVAL_POLICY_PROPOSALS_INDEX_URI}/{proposal_id}")
>>>  218:         self.notify_proposals_changed()
>>>  219:         # Also notify agent-specific policy state resource since proposals changed
>>>  220:         self.notify_resource(AGENTS_POLICY_STATE_URI_FMT.format(agent_id=self.agent_id))
     221:
     222:     async def create_proposal(self, content: str) -> int:
     223:         """Create a new policy proposal and return its ID.
   ...
     233:         await self.persistence.create_policy_proposal(self.agent_id, proposal_id=new_id, content=content)
     234:         # Note: We don't have the actual ID here, but persistence will handle it
     235:         # For now, notify with string version for compatibility
>>>  236:         self.notify_proposal_change(str(new_id))
     237:         return new_id
     238:
     239:     async def approve_proposal(self, proposal_id: int) -> None:
   ...
     250:         # Activate policy (notifies via engine's set_policy)
     251:         self.set_policy(got.content)
     252:         await self.persistence.approve_policy_proposal(self.agent_id, proposal_id)
>>>  253:         self.notify_proposal_change(str(proposal_id))
     254:
     255:     async def reject_proposal(self, proposal_id: int) -> None:
     256:         """Reject a pending policy proposal by ID."""
     257:         await self.persistence.reject_policy_proposal(self.agent_id, proposal_id)
>>>  258:         self.notify_proposal_change(str(proposal_id))
     259:
     260:
     261: def make_policy_engine(
```

### `proposal-notifies-wrong-id.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/issues/proposal-notifies-wrong-id.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/code/adgn/src/adgn/agent/approvals.py#L232-L237)

File: `adgn/src/adgn/agent/approvals.py` (L232-237, L234-235)

> Lines 232-237 define create_proposal that sets new_id = 0 as placeholder, calls
> persistence with that placeholder, and notifies with str(new_id) still as "0".
> The actual database-assigned ID is never retrieved or used.
>
> Bug: clients receiving the notification get wrong proposal ID (0), notification
> points to non-existent proposal, return value at line 237 also wrong (returns 0
> instead of actual ID), creates data inconsistency between notified and persisted.
>
> Fix: create_policy_proposal should return actual database-assigned ID, then notify
> and return that ID. Or if persistence doesn't return ID, refactor it to do so or
> query for newly created proposal. Comment at lines 234-235 acknowledges the problem.
>
> Related to issue 023 about proposal_id type inconsistency.

```
     229:         if self.docker_client is not None:
     230:             self.self_check(content)
     231:         # Generate new proposal ID (will be auto-generated by DB, use placeholder)
>>>  232:         new_id = 0  # Placeholder, actual ID assigned by database
>>>  233:         await self.persistence.create_policy_proposal(self.agent_id, proposal_id=new_id, content=content)
>>>  234:         # Note: We don't have the actual ID here, but persistence will handle it
>>>  235:         # For now, notify with string version for compatibility
>>>  236:         self.notify_proposal_change(str(new_id))
>>>  237:         return new_id
     238:
     239:     async def approve_proposal(self, proposal_id: int) -> None:
     240:         """Approve a pending policy proposal by ID and activate it.
```

### `test-main-block.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/issues/test-main-block.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/code/adgn/tests/agent/test_policy_validation_reload.py#L153-L154)

File: `adgn/tests/agent/test_policy_validation_reload.py` (L153-154)

> Test file has unnecessary `__main__` block.
>
> Lines 153-154 in test_policy_validation_reload.py contain:
>
> ```python
> if __name__ == "__main__":
>     pytest.main([__file__, "-v"])
> ```
>
> Pytest tests shouldn't have `__main__` blocks. Run with `pytest` command instead. This is an outdated pattern.

```
     150:         await new_admin_server._mcp_server._tools["reload_policy"].fn(ReloadPolicyArgs(source=None))
     151:
     152:
>>>  153: if __name__ == "__main__":
>>>  154:     pytest.main([__file__, "-v"])
```

### `tests-nonexistent-api.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/issues/tests-nonexistent-api.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-20-01/code/adgn/tests/mcp/approval_policy/test_policy_resources.py#L1-L384)

File: `adgn/tests/mcp/approval_policy/test_policy_resources.py` (L1-384)

> The test file `adgn/tests/mcp/approval_policy/test_policy_resources.py` tests a policy
> CRUD API that was never implemented in the production code.
>
> **Problem:**
> The test imports and uses types that don't exist in the codebase:
>
> - `CreatePolicyArgs` - for creating policies via admin tools
> - `UpdatePolicyArgs` - for updating policies
> - `DeletePolicyArgs` - for deleting policies
>
> These test a full policy CRUD (Create, Read, Update, Delete) API that was apparently
> planned but never implemented. The actual ApprovalPolicyAdminServer only provides:
>
> - Proposal management: create_proposal, approve_proposal, reject_proposal
> - Policy operations: set_policy, validate_policy, reload_policy
>
> There is no separate "create_policy", "update_policy", or "delete_policy" tool/functionality.
> The test file appears to be a placeholder or leftover from an earlier design.
>
> **Evidence:**
>
> - Test imports CreatePolicyArgs, UpdatePolicyArgs, DeletePolicyArgs (line 11-14)
> - All test classes (TestPolicyListResource, TestPolicyDetailResource, TestCreatePolicyTool,
>   TestUpdatePolicyTool, TestDeletePolicyTool, TestPolicyPagination, TestErrorHandling)
>   reference these non-existent types
> - The production code uses a different model: policies are managed through proposals
>   (create → approve) rather than direct CRUD operations
>
> **Resolution:**
> Delete this test file entirely. It tests functionality that was never built and would
> require significant production code implementation to make valid. The actual policy
> functionality is tested in test_policy_validation_reload.py and test_proposals_resources.py.
>
> **Alternative considerations:**
>
> - If direct policy CRUD is desired, it should be implemented in production code first
> - The test could be kept as a specification/TODO, but it's confusing to have failing
>   tests for unimplemented features in the main test suite
> - Better to track this as a feature request in documentation rather than broken tests

```
>>>    1: """Tests for policy CRUD resources and tools in the approval policy MCP server."""
>>>    2:
>>>    3: from __future__ import annotations
>>>    4:
>>>    5: from docker import DockerClient
>>>    6: import pytest
>>>    7:
>>>    8: from adgn.agent.approvals import ApprovalPolicyEngine, load_default_policy_source
>>>    9: from adgn.agent.persist.sqlite import SQLitePersistence
>>>   10: from adgn.mcp.approval_policy.server import (
>>>   11:     ApprovalPolicyAdminServer,
>>>   12:     ApprovalPolicyServer,
>>>   13:     CreatePolicyArgs,
>>>   14:     DeletePolicyArgs,
>>>   15:     UpdatePolicyArgs,
>>>   16: )
>>>   17:
>>>   18:
>>>   19: @pytest.fixture
>>>   20: async def persistence(tmp_path):
>>>   21:     """Create a temporary SQLite persistence instance."""
>>>   22:     db_path = tmp_path / "test.db"
>>>   23:     persist = SQLitePersistence(db_path)
>>>   24:     await persist.ensure_schema()
>>>   25:     return persist
>>>   26:
>>>   27:
>>>   28: @pytest.fixture
>>>   29: async def engine(persistence, docker_client: DockerClient):
>>>   30:     """Create an approval policy engine with test persistence."""
>>>   31:
>>>   32:     agent_id = "test-agent"
>>>   33:
>>>   34:     # Create agent in persistence
>>>   35:     from fastmcp.mcp_config import MCPConfig
>>>   36:
>>>   37:     from adgn.agent.persist import AgentMetadata
>>>   38:
>>>   39:     await persistence.create_agent(mcp_config=MCPConfig(), metadata=AgentMetadata(preset="test"))
>>>   40:
>>>   41:     # Create engine with default policy
>>>   42:     policy_source = load_default_policy_source()
>>>   43:     engine = ApprovalPolicyEngine(
>>>   44:         docker_client=docker_client,
>>>   45:         agent_id=agent_id,
>>>   46:         persistence=persistence,
>>>   47:         policy_source=policy_source,
>>>   48:     )
>>>   49:     return engine
>>>   50:
>>>   51:
>>>   52: @pytest.fixture
>>>   53: async def policy_server(engine):
>>>   54:     """Create a policy server (reader) instance."""
>>>   55:     return ApprovalPolicyServer(engine)
>>>   56:
>>>   57:
>>>   58: @pytest.fixture
>>>   59: async def admin_server(engine):
>>>   60:     """Create an admin server instance."""
>>>   61:     return ApprovalPolicyAdminServer(engine=engine)
>>>   62:
>>>   63:
>>>   64: class TestPolicyListResource:
>>>   65:     """Test the policy list resource."""
>>>   66:
>>>   67:     async def test_list_empty(self, policy_server):
>>>   68:         """Test listing policies when none exist."""
>>>   69:         # Access the policies_list resource
>>>   70:         result = await policy_server._mcp_server.read_resource(uri="resource://policies/list")
>>>   71:         assert result is not None
>>>   72:         # Should return empty list as JSON
>>>   73:         import json
>>>   74:
>>>   75:         data = json.loads(result.contents[0].text)
>>>   76:         assert isinstance(data, list)
>>>   77:         assert len(data) == 0
>>>   78:
>>>   79:     async def test_list_with_policies(self, policy_server, admin_server, persistence):
>>>   80:         """Test listing policies after creating some."""
>>>   81:         # Create a few policies via admin tools
>>>   82:         policy1 = await admin_server._mcp_server.call_tool(
>>>   83:             "create_policy",
>>>   84:             arguments=CreatePolicyArgs(
>>>   85:                 id="policy-1",
>>>   86:                 text="print('policy 1')",
>>>   87:                 description="First test policy",
>>>   88:                 enabled=True,
>>>   89:             ).model_dump(),
>>>   90:         )
>>>   91:
>>>   92:         policy2 = await admin_server._mcp_server.call_tool(
>>>   93:             "create_policy",
>>>   94:             arguments=CreatePolicyArgs(
>>>   95:                 id="policy-2",
>>>   96:                 text="print('policy 2')",
>>>   97:                 description="Second test policy",
>>>   98:                 enabled=False,
>>>   99:             ).model_dump(),
>>>  100:         )
>>>  101:
>>>  102:         # Now list policies
>>>  103:         result = await policy_server._mcp_server.read_resource(uri="resource://policies/list")
>>>  104:         assert result is not None
>>>  105:
>>>  106:         import json
>>>  107:
>>>  108:         data = json.loads(result.contents[0].text)
>>>  109:         assert isinstance(data, list)
>>>  110:         assert len(data) == 2
>>>  111:
>>>  112:         # Verify structure (should be PolicyListItem)
>>>  113:         for item in data:
>>>  114:             assert "id" in item
>>>  115:             assert "description" in item
>>>  116:             assert "enabled" in item
>>>  117:
>>>  118:
>>>  119: class TestPolicyDetailResource:
>>>  120:     """Test the policy detail resource."""
>>>  121:
>>>  122:     async def test_get_nonexistent(self, policy_server):
>>>  123:         """Test getting a policy that doesn't exist."""
>>>  124:         with pytest.raises(KeyError):
>>>  125:             await policy_server._mcp_server.read_resource(uri="resource://policies/nonexistent")
>>>  126:
>>>  127:     async def test_get_existing(self, policy_server, admin_server):
>>>  128:         """Test getting an existing policy."""
>>>  129:         # Create a policy first
>>>  130:         await admin_server._mcp_server.call_tool(
>>>  131:             "create_policy",
>>>  132:             arguments=CreatePolicyArgs(
>>>  133:                 id="test-policy",
>>>  134:                 text="print('test policy')",
>>>  135:                 description="A test policy",
>>>  136:                 enabled=True,
>>>  137:             ).model_dump(),
>>>  138:         )
>>>  139:
>>>  140:         # Now get it
>>>  141:         result = await policy_server._mcp_server.read_resource(uri="resource://policies/test-policy")
>>>  142:         assert result is not None
>>>  143:
>>>  144:         import json
>>>  145:
>>>  146:         data = json.loads(result.contents[0].text)
>>>  147:         assert data["id"] == "test-policy"
>>>  148:         assert data["text"] == "print('test policy')"
>>>  149:         assert data["description"] == "A test policy"
>>>  150:         assert data["enabled"] is True
>>>  151:
>>>  152:
>>>  153: class TestCreatePolicyTool:
>>>  154:     """Test the create_policy admin tool."""
>>>  155:
>>>  156:     async def test_create_basic(self, admin_server, persistence):
>>>  157:         """Test creating a basic policy."""
>>>  158:         result = await admin_server._mcp_server.call_tool(
>>>  159:             "create_policy",
>>>  160:             arguments=CreatePolicyArgs(
>>>  161:                 id="new-policy",
>>>  162:                 text="print('new policy')",
>>>  163:                 description="A new policy",
>>>  164:                 enabled=True,
>>>  165:             ).model_dump(),
>>>  166:         )
>>>  167:
>>>  168:         assert result.isError is False
>>>  169:
>>>  170:         # Verify it was created in persistence
>>>  171:         policy = await persistence.get_policy("new-policy")
>>>  172:         assert policy is not None
>>>  173:         assert policy.id == "new-policy"
>>>  174:         assert policy.text == "print('new policy')"
>>>  175:         assert policy.description == "A new policy"
>>>  176:         assert policy.enabled is True
>>>  177:
>>>  178:     async def test_create_duplicate(self, admin_server, persistence):
>>>  179:         """Test creating a policy with duplicate ID fails."""
>>>  180:         # Create first policy
>>>  181:         await admin_server._mcp_server.call_tool(
>>>  182:             "create_policy",
>>>  183:             arguments=CreatePolicyArgs(
>>>  184:                 id="dup-policy",
>>>  185:                 text="print('dup')",
>>>  186:             ).model_dump(),
>>>  187:         )
>>>  188:
>>>  189:         # Try to create another with same ID
>>>  190:         result = await admin_server._mcp_server.call_tool(
>>>  191:             "create_policy",
>>>  192:             arguments=CreatePolicyArgs(
>>>  193:                 id="dup-policy",
>>>  194:                 text="print('dup 2')",
>>>  195:             ).model_dump(),
>>>  196:             raise_on_error=False,
>>>  197:         )
>>>  198:
>>>  199:         assert result.isError is True
>>>  200:
>>>  201:     async def test_create_minimal(self, admin_server, persistence):
>>>  202:         """Test creating a policy with minimal args."""
>>>  203:         result = await admin_server._mcp_server.call_tool(
>>>  204:             "create_policy",
>>>  205:             arguments=CreatePolicyArgs(
>>>  206:                 id="minimal",
>>>  207:                 text="pass",
>>>  208:             ).model_dump(),
>>>  209:         )
>>>  210:
>>>  211:         assert result.isError is False
>>>  212:
>>>  213:         policy = await persistence.get_policy("minimal")
>>>  214:         assert policy is not None
>>>  215:         assert policy.id == "minimal"
>>>  216:         assert policy.text == "pass"
>>>  217:         assert policy.description is None
>>>  218:         assert policy.enabled is True  # default
>>>  219:
>>>  220:
>>>  221: class TestUpdatePolicyTool:
>>>  222:     """Test the update_policy admin tool."""
>>>  223:
>>>  224:     async def test_update_existing(self, admin_server, persistence):
>>>  225:         """Test updating an existing policy."""
>>>  226:         # Create a policy first
>>>  227:         await admin_server._mcp_server.call_tool(
>>>  228:             "create_policy",
>>>  229:             arguments=CreatePolicyArgs(
>>>  230:                 id="update-me",
>>>  231:                 text="print('v1')",
>>>  232:                 description="Version 1",
>>>  233:             ).model_dump(),
>>>  234:         )
>>>  235:
>>>  236:         # Update it
>>>  237:         result = await admin_server._mcp_server.call_tool(
>>>  238:             "update_policy",
>>>  239:             arguments=UpdatePolicyArgs(
>>>  240:                 id="update-me",
>>>  241:                 text="print('v2')",
>>>  242:                 description="Version 2",
>>>  243:             ).model_dump(),
>>>  244:         )
>>>  245:
>>>  246:         assert result.isError is False
>>>  247:
>>>  248:         # Verify the update
>>>  249:         policy = await persistence.get_policy("update-me")
>>>  250:         assert policy is not None
>>>  251:         assert policy.text == "print('v2')"
>>>  252:         assert policy.description == "Version 2"
>>>  253:
>>>  254:     async def test_update_nonexistent(self, admin_server):
>>>  255:         """Test updating a nonexistent policy fails."""
>>>  256:         result = await admin_server._mcp_server.call_tool(
>>>  257:             "update_policy",
>>>  258:             arguments=UpdatePolicyArgs(
>>>  259:                 id="nonexistent",
>>>  260:                 text="print('new')",
>>>  261:             ).model_dump(),
>>>  262:             raise_on_error=False,
>>>  263:         )
>>>  264:
>>>  265:         assert result.isError is True
>>>  266:
>>>  267:     async def test_update_creates_history(self, admin_server, persistence):
>>>  268:         """Test that updating a policy creates a history entry."""
>>>  269:         # Create initial policy
>>>  270:         await admin_server._mcp_server.call_tool(
>>>  271:             "create_policy",
>>>  272:             arguments=CreatePolicyArgs(
>>>  273:                 id="versioned",
>>>  274:                 text="print('v1')",
>>>  275:             ).model_dump(),
>>>  276:         )
>>>  277:
>>>  278:         # Update it
>>>  279:         await admin_server._mcp_server.call_tool(
>>>  280:             "update_policy",
>>>  281:             arguments=UpdatePolicyArgs(
>>>  282:                 id="versioned",
>>>  283:                 text="print('v2')",
>>>  284:             ).model_dump(),
>>>  285:         )
>>>  286:
>>>  287:         # Check that history was created (requires accessing policy_history table)
>>>  288:         # For now, just verify the update worked
>>>  289:         policy = await persistence.get_policy("versioned")
>>>  290:         assert policy.text == "print('v2')"
>>>  291:
>>>  292:
>>>  293: class TestDeletePolicyTool:
>>>  294:     """Test the delete_policy admin tool."""
>>>  295:
>>>  296:     async def test_delete_existing(self, admin_server, persistence):
>>>  297:         """Test deleting an existing policy."""
>>>  298:         # Create a policy first
>>>  299:         await admin_server._mcp_server.call_tool(
>>>  300:             "create_policy",
>>>  301:             arguments=CreatePolicyArgs(
>>>  302:                 id="delete-me",
>>>  303:                 text="print('bye')",
>>>  304:             ).model_dump(),
>>>  305:         )
>>>  306:
>>>  307:         # Verify it exists
>>>  308:         policy = await persistence.get_policy("delete-me")
>>>  309:         assert policy is not None
>>>  310:
>>>  311:         # Delete it
>>>  312:         result = await admin_server._mcp_server.call_tool(
>>>  313:             "delete_policy",
>>>  314:             arguments=DeletePolicyArgs(id="delete-me").model_dump(),
>>>  315:         )
>>>  316:
>>>  317:         assert result.isError is False
>>>  318:
>>>  319:         # Verify it's gone
>>>  320:         policy = await persistence.get_policy("delete-me")
>>>  321:         assert policy is None
>>>  322:
>>>  323:     async def test_delete_nonexistent(self, admin_server):
>>>  324:         """Test deleting a nonexistent policy succeeds (idempotent)."""
>>>  325:         result = await admin_server._mcp_server.call_tool(
>>>  326:             "delete_policy",
>>>  327:             arguments=DeletePolicyArgs(id="nonexistent").model_dump(),
>>>  328:         )
>>>  329:
>>>  330:         # SQLite DELETE is idempotent, so this should succeed
>>>  331:         assert result.isError is False
>>>  332:
>>>  333:
>>>  334: class TestPolicyPagination:
>>>  335:     """Test pagination in policy list."""
>>>  336:
>>>  337:     async def test_pagination(self, admin_server, persistence):
>>>  338:         """Test that pagination works for policy list."""
>>>  339:         # Create multiple policies
>>>  340:         for i in range(10):
>>>  341:             await persistence.create_policy(
>>>  342:                 policy_id=f"policy-{i}",
>>>  343:                 text=f"print({i})",
>>>  344:                 description=f"Policy {i}",
>>>  345:             )
>>>  346:
>>>  347:         # List with limit
>>>  348:         policies = await persistence.list_policies(offset=0, limit=5)
>>>  349:         assert len(policies) == 5
>>>  350:
>>>  351:         # List next page
>>>  352:         policies = await persistence.list_policies(offset=5, limit=5)
>>>  353:         assert len(policies) == 5
>>>  354:
>>>  355:         # List all
>>>  356:         policies = await persistence.list_policies(offset=0, limit=100)
>>>  357:         assert len(policies) == 10
>>>  358:
>>>  359:
>>>  360: class TestErrorHandling:
>>>  361:     """Test error handling in policy CRUD operations."""
>>>  362:
>>>  363:     async def test_invalid_policy_text(self, admin_server):
>>>  364:         """Test that invalid Python syntax is caught."""
>>>  365:         # Note: create_policy doesn't validate syntax, so this should succeed
>>>  366:         result = await admin_server._mcp_server.call_tool(
>>>  367:             "create_policy",
>>>  368:             arguments=CreatePolicyArgs(
>>>  369:                 id="invalid",
>>>  370:                 text="this is not valid python !!!",
>>>  371:             ).model_dump(),
>>>  372:         )
>>>  373:
>>>  374:         # Creation succeeds (validation happens at execution time)
>>>  375:         assert result.isError is False
>>>  376:
>>>  377:     async def test_missing_required_fields(self, admin_server):
>>>  378:         """Test that missing required fields cause validation errors."""
>>>  379:         # Missing 'id' and 'text'
>>>  380:         with pytest.raises(Exception):  # Pydantic validation error
>>>  381:             await admin_server._mcp_server.call_tool(
>>>  382:                 "create_policy",
>>>  383:                 arguments={},  # Missing required fields
>>>  384:             )
```

## ducktape/2025-11-26-00 (5)

### `ask-approved-inflight.yaml` / `occ-0` [P20]

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-26-00/issues/ask-approved-inflight.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-26-00/code/adgn/src/adgn/mcp/policy_gateway/middleware.py#L167-L258)

File: `adgn/src/adgn/mcp/policy_gateway/middleware.py` (L252-258, L167-225)

> When user approves an ASK-case tool call (ContinueDecision at lines 252-258), middleware executes it but does
> NOT
> track
> it in `self._inflight`, making it invisible to `has_inflight_calls()` and `inflight_count()`.
>
> The ALLOW case (lines 167-225) correctly tracks in \_inflight during execution with try/finally cleanup.
>
> Problems: (1) `has_inflight_calls()` returns False even when ASK-approved call is executing, (2)
> `inflight_count()`
> doesn't count ASK-approved calls, (3) can't distinguish "waiting for approval" vs "approved and executing",
> (4)
> inconsistent tracking between ALLOW and ASK paths.
>
> Match the ALLOW pattern: add call to \_inflight before execution, clean up in finally block. Both paths should
> track
> consistently regardless of whether policy allowed or user approved.

```
     164:                 await self._record("pg:" + uuid.uuid4().hex, tool_key, ApprovalOutcome.POLICY_ALLOW)
     165:
     166:             # Track in-flight tool call
>>>  167:             call_id = uuid.uuid4().hex
>>>  168:             self._inflight[call_id] = tool_key
>>>  169:             try:
>>>  170:                 call_result = await call_next(context)
>>>  171:                 # If downstream returned an error ToolResult instead of raising,
>>>  172:                 # remap reserved policy codes/messages here using typed parsing when available.
>>>  173:                 if bool(getattr(call_result, "is_error", False)):
>>>  174:                     # Parse error details - ErrorData guarantees code: int per MCP/JSON-RPC spec
>>>  175:                     err = getattr(call_result, "error", None)
>>>  176:                     if err is None:
>>>  177:                         return call_result
>>>  178:
>>>  179:                     # Try parsing as ErrorData (validates code is int, message is str)
>>>  180:                     try:
>>>  181:                         ed = mtypes.ErrorData.model_validate(err)
>>>  182:                     except Exception:
>>>  183:                         # Non-conforming error format - pass through
>>>  184:                         return call_result
>>>  185:
>>>  186:                     # Check if error uses reserved policy codes/messages
>>>  187:                     stamped_downstream = isinstance(ed.data, dict) and ed.data.get(POLICY_GATEWAY_STAMP_KEY) is True
>>>  188:                     if (
>>>  189:                         stamped_downstream
>>>  190:                         or ed.code
>>>  191:                         in (POLICY_DENIED_ABORT_CODE, POLICY_DENIED_CONTINUE_CODE, POLICY_EVALUATOR_ERROR_CODE)
>>>  192:                         or ed.message
>>>  193:                         in (POLICY_DENIED_ABORT_MSG, POLICY_DENIED_CONTINUE_MSG, POLICY_EVALUATOR_ERROR_MSG)
>>>  194:                     ):
>>>  195:                         raise McpError(
>>>  196:                             ErrorData(
>>>  197:                                 code=POLICY_BACKEND_RESERVED_MISUSE_CODE,
>>>  198:                                 message=POLICY_BACKEND_RESERVED_MISUSE_MSG,
>>>  199:                                 data={POLICY_GATEWAY_STAMP_KEY: True, "name": name, "backend_code": ed.code},
>>>  200:                             )
>>>  201:                         )
>>>  202:                 return call_result
>>>  203:             except McpError as e:
>>>  204:                 _raise_if_reserved_code(e, name)
>>>  205:                 raise
>>>  206:             except Exception as e:
>>>  207:                 # Some servers may translate backend McpError into a ToolError before it reaches us.
>>>  208:                 # As a last resort, remap by inspecting the exception text.
>>>  209:                 s = str(e)
>>>  210:                 if (
>>>  211:                     (POLICY_DENIED_ABORT_MSG in s)
>>>  212:                     or (POLICY_DENIED_CONTINUE_MSG in s)
>>>  213:                     or (POLICY_EVALUATOR_ERROR_MSG in s)
>>>  214:                 ):
>>>  215:                     raise McpError(
>>>  216:                         ErrorData(
>>>  217:                             code=POLICY_BACKEND_RESERVED_MISUSE_CODE,
>>>  218:                             message=POLICY_BACKEND_RESERVED_MISUSE_MSG,
>>>  219:                             data={POLICY_GATEWAY_STAMP_KEY: True, "name": name, "backend_code": "unknown"},
>>>  220:                         )
>>>  221:                     )
>>>  222:                 raise
>>>  223:             finally:
>>>  224:                 # Remove from in-flight tracking when call completes (success or error)
>>>  225:                 self._inflight.pop(call_id, None)
     226:
     227:         if decision is ApprovalDecision.DENY_ABORT:
     228:             if self._record is not None:
   ...
     249:
     250:         decision_obj = await wait_coro
     251:
>>>  252:         if isinstance(decision_obj, ContinueDecision):
>>>  253:             if self._record is not None:
>>>  254:                 await self._record(call_id, tool_key, ApprovalOutcome.POLICY_ALLOW)
>>>  255:             try:
>>>  256:                 return await call_next(context)
>>>  257:             except McpError as e:
>>>  258:                 _raise_if_reserved_code(e, name)
     259:                 raise
     260:         if isinstance(decision_obj, AbortTurnDecision):
     261:             if self._record is not None:
```

### `mutable-batch-accumulation.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-26-00/issues/mutable-batch-accumulation.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-26-00/code/adgn/src/adgn/mcp/notifications/buffer.py#L40-L41)

File: `adgn/src/adgn/mcp/notifications/buffer.py` (L40-41)

> The class uses sets (`_updates`, `_list_changed`) during accumulation, then converts
> to frozen structures in NotificationsBatch. This is clunky.
>
> **Current pattern:**
>
> ```python
> # Accumulation storage (mutable sets)
> self._updates: dict[str, set[str]] = {}
> self._list_changed: set[str] = set()
>
> # On add:
> self._updates[server_name].add(uri)
> self._list_changed.add(server_name)
>
> # On poll/peek:
> resources = self._build_resources()  # Converts sets to frozen structures
> return NotificationsBatch(resources=resources)
> ```
>
> **Better approach:**
> Replace `dict[str, set[str]]` and `set[str]` with a single mutable `NotificationsBatch`
> instance (`self._batch`). On add operations, mutate `_batch` directly. On poll, return
> `self._batch.model_copy()` and reset `_batch = NotificationsBatch()`. On peek, return
> `self._batch.model_copy()`. This eliminates the conversion logic between sets and frozen
> structures.

```
      37:     - Hooks can be registered to react to updates (e.g., push UI snapshots).
      38:     """
      39:
>>>   40:     def __init__(self, *, client: Client | None = None, compositor: Compositor) -> None:
>>>   41:         self._client = client
      42:         self._compositor = compositor
      43:         # Per-server updates (mutable sets during accumulation, converted to frozenset on poll/peek)
      44:         self._updates: dict[str, set[str]] = {}
```

### `poll-use-peek.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-26-00/issues/poll-use-peek.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-26-00/code/adgn/src/adgn/mcp/notifications/buffer.py#L62-L72)

File: `adgn/src/adgn/mcp/notifications/buffer.py` (L62-72)

> Lines 62-72 define `poll()` and `peek()` which both call `_build_resources()` and
> create `NotificationsBatch` objects independently. This duplicates the batch creation
> logic.
>
> **The issue:** Both methods build resources and construct batch objects separately,
> obscuring that `poll()` is conceptually `peek()` plus clear operations.
>
> **Fix:** Make `poll()` call `peek()`, then clear buffers. This DRYs batch creation
> into one place and makes the relationship explicit: poll = peek + clear.
>
> If `_build_resources()` becomes single-use after this change, inline it into `peek()`.

```
      59:     def clear_hooks(self) -> None:
      60:         self._hooks.clear()
      61:
>>>   62:     def poll(self) -> NotificationsBatch:
>>>   63:         """Poll and clear buffered notifications, returning grouped batch."""
>>>   64:         resources = self._build_resources()
>>>   65:         self._updates.clear()
>>>   66:         self._list_changed.clear()
>>>   67:         return NotificationsBatch(resources=resources)
>>>   68:
>>>   69:     def peek(self) -> NotificationsBatch:
>>>   70:         """Peek at buffered notifications without clearing them."""
>>>   71:         resources = self._build_resources()
>>>   72:         return NotificationsBatch(resources=resources)
      73:
      74:     def _build_resources(self) -> dict[str, ResourcesServerNotice]:
      75:         """Build the grouped resources structure from current buffer state."""
```

### `resources-take-client.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-26-00/issues/resources-take-client.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-26-00/code/adgn/src/adgn/mcp/resources/server.py#L238-L241)

File: `adgn/src/adgn/mcp/resources/server.py` (L238-241)

> Lines 238-241 create `Client(compositor)` internally, but resources server should receive Client as parameter
> instead
> of
> Compositor.
>
> Violates "take what you need" principle (Dependency Injection): (1) server receives Compositor but only uses
> it to
> create Client, (2) creates client internally instead of receiving it, (3) harder to test (can't inject
> mock/test
> client).
>
> Change signature to `make_resources_server(name: str, client: Client)` and use client directly. Caller creates
> Client
> and passes it. Delete useless comments about "bypassing policy gateway" (lines 238-240); parameter docstring
> should
> explain this instead. Benefits: takes what it needs, easier to test, clearer dependencies, follows standard
> DI.

```
     235:         name, instructions=("Resources aggregator for listing/reading resources across mounted servers.")
     236:     )
     237:
>>>  238:     # Direct client to compositor (bypasses policy gateway to prevent double enforcement)
>>>  239:     # This client is created without middleware since tools calling this server already
>>>  240:     # went through the policy gateway
>>>  241:     compositor_client = Client(compositor)
     242:
     243:     # ---- Subscriptions index (single resource) -----------------------------
     244:     # Internal store for subscriptions made via this server's subscribe tool.
```

### `truncation-noop.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-26-00/issues/truncation-noop.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-26-00/code/adgn/src/adgn/inop/prompting/truncation_utils.py#L36-L181)

File: `adgn/src/adgn/inop/prompting/truncation_utils.py` (L36-40, L162-181)

> `truncate_file_by_bytes` (line 162) calls
> `self._truncated_content(content, len(content))` at line 178.
> `_truncated_content` (line 36) returns `content` unchanged when
> `len(content) <= max_chars`. Since `max_chars` equals `len(content)`, the
> condition is always true — the call is equivalent to an identity function
> and the truncation suffix is never appended.

```
      33:             return text
      34:         return text[: max_length - len(suffix)] + suffix
      35:
>>>   36:     def _truncated_content(self, content: str, max_chars: int) -> str:
>>>   37:         """Helper to create truncated content with standard message."""
>>>   38:         if len(content) <= max_chars:
>>>   39:             return content
>>>   40:         return content[:max_chars] + f"\n... [TRUNCATED: {len(content)} chars total, showing first {max_chars}]"
      41:
      42:     def _skipped_content(self, content: str, threshold: int) -> str:
      43:         """Helper to create skipped content message."""
   ...
     159:         assert final <= max_tokens, f"File truncation failed: {final} tokens > {max_tokens} limit"
     160:         return result
     161:
>>>  162:     def truncate_file_by_bytes(self, file_path: Path, max_bytes: int) -> str:
>>>  163:         """Read and truncate a single file by byte size.
>>>  164:
>>>  165:         Args:
>>>  166:             file_path: Path to the file
>>>  167:             max_bytes: Maximum bytes to read
>>>  168:
>>>  169:         Returns:
>>>  170:             File content, possibly truncated
>>>  171:         """
>>>  172:         try:
>>>  173:             file_size = file_path.stat().st_size
>>>  174:
>>>  175:             if file_size > max_bytes:
>>>  176:                 with file_path.open("r", encoding="utf-8") as f:
>>>  177:                     content = f.read(max_bytes)
>>>  178:                 return self._truncated_content(content, len(content))  # Will show the truncation message
>>>  179:             return file_path.read_text()
>>>  180:         except UnicodeDecodeError:
>>>  181:             return "<<not a plaintext file>>"
```

## ducktape/2025-11-21-00 (3)

### `agent-info-computable-uris.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-21-00/issues/agent-info-computable-uris.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-21-00/code/adgn/src/adgn/agent/mcp_bridge/servers/agents.py#L144-L146)

File: `adgn/src/adgn/agent/mcp_bridge/servers/agents.py` (L144-146)

> The `state_uri`, `approvals_uri`, and `policy_proposals_uri` fields in `AgentInfo` (lines 144-146)
> can always be computed from `agent_id`. They should not be in the Pydantic model as they add no
> information and create unnecessary redundancy.
>
> URIs follow deterministic patterns: `resource://agents/{agent_id}/policy/state`,
> `resource://agents/{agent_id}/approvals/history`, `resource://agents/{agent_id}/policy/proposals`.
> Client can easily construct given agent_id.
>
> Problems: (1) Storing precomputed derivable values violates DRY, creates maintenance burden.
> (2) If URI patterns change, must update both construction logic AND field values. (3) All three
> are `str | None = None`, but could always be computed - `None` default misleadingly suggests
> sometimes unavailable. (4) Fields appear defined but not populated anywhere (no assignments
> found), dead weight. (5) Bloats response payloads with redundant URIs.
>
> Fix: Remove all three URI fields from AgentInfo. If clients need URIs, construct client-side
> from `agent_id` using helper, or use separate endpoint. Alternative: `@property` that computes
> on-demand, but removing entirely is preferred.
>
> Benefits: Single source of truth for URI patterns, smaller cleaner model, no risk of stale URIs,
> less code to maintain, clearer URIs are derived not stored.

```
     141:     """Information about a single agent."""
     142:
     143:     agent_id: AgentID
>>>  144:     capabilities: dict[str, bool]  # e.g., {"chat": True, "agent_loop": False}
>>>  145:     mode: AgentMode
>>>  146:     state_uri: str | None = None
     147:     approvals_uri: str | None = None
     148:     policy_proposals_uri: str | None = None
     149:
```

### `approvals-pending-manual-json.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-21-00/issues/approvals-pending-manual-json.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-21-00/code/adgn/src/adgn/agent/mcp_bridge/servers/agents.py#L395-L424)

File: `adgn/src/adgn/agent/mcp_bridge/servers/agents.py` (L395-424, L411-419, L421-424)

> Lines 395-424 define `approvals_pending_global` that manually constructs JSON dicts with
> string keys and `json.dumps()` instead of using Pydantic models.
>
> Problems: manual dict construction doesn't catch typos (`{"call_idd": x}`); no validation
> (wrong types like `{"call_id": 123}` slip through); hard to evolve (field changes require
> manual updates across dict literals); inconsistent with codebase (other functions use
> Pydantic like AgentApprovalsPending); nested tool_call dict manually constructed despite
> existing ToolCall model; no IDE autocomplete or type checking.
>
> Lines 411-419 manually build pending_list dicts; lines 421-424 manually construct result
> dicts with json.dumps.
>
> Replace with Pydantic models (PendingApprovalItem, AgentPendingApprovalsBlock, ResourceBlock)
> and use model_dump_json() for serialization. Benefits: type safety, automatic validation,
> IDE support, reuses existing ToolCall model, framework handles serialization.

```
     392:         "resource://approvals/pending",
     393:         name="approvals.pending.global",
     394:         mime_type="application/json",
>>>  395:         description="Global mailbox: all pending approvals across all agents (returns multiple content blocks)",
>>>  396:     )
>>>  397:     async def approvals_pending_global():
>>>  398:         """Each approval is a separate MCP TextResourceContents block.
>>>  399:
>>>  400:         Crashes if any agent fails (no exception swallowing).
>>>  401:         """
>>>  402:         content_blocks: list[mcp_types.TextResourceContents] = []
>>>  403:
>>>  404:         for agent_id in registry.known_agents():
>>>  405:             infra = await registry.get_infrastructure(agent_id)
>>>  406:             pending_approvals = _convert_pending_approvals(infra.approval_hub.pending)
>>>  407:
>>>  408:             for approval in pending_approvals:
>>>  409:                 approval_uri = f"resource://agents/{agent_id}/approvals/{approval.call_id}"
>>>  410:                 approval_data = {
>>>  411:                     "agent_id": agent_id,
>>>  412:                     "call_id": approval.call_id,
>>>  413:                     "tool": approval.tool,
>>>  414:                     "args": approval.args,
>>>  415:                     "timestamp": approval.timestamp.isoformat(),
>>>  416:                 }
>>>  417:                 block = mcp_types.TextResourceContents(
>>>  418:                     uri=approval_uri, mimeType="application/json", text=json.dumps(approval_data)
>>>  419:                 )
>>>  420:                 content_blocks.append(block)
>>>  421:
>>>  422:         return mcp_types.ReadResourceResult(contents=content_blocks)
>>>  423:
>>>  424:     @server.resource(
     425:         "resource://agents/{agent_id}/approvals/history",
     426:         name="agent.approvals.history",
     427:         mime_type="application/json",
```

### `proposal-uri-computable.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-21-00/issues/proposal-uri-computable.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-21-00/code/adgn/src/adgn/agent/mcp_bridge/servers/agents.py#L178)

File: `adgn/src/adgn/agent/mcp_bridge/servers/agents.py` (L178)

> Line 178 defines `PolicyProposalInfo` with a `proposal_uri` field that is trivially computable
> from the `id` field via `f"{APPROVAL_POLICY_PROPOSALS_INDEX_URI}/{id}"`.
>
> This creates redundancy and inconsistency risk: storing both `id` and `proposal_uri` violates
> DRY when one is derivable from the other. If the URI pattern changes, both the construction
> logic and this field must be updated. The field also bloats response payloads when listing
> many proposals.
>
> The codebase uses IDs as primary identifiers elsewhere, not URIs. Mixing both creates
> confusion about which is canonical.
>
> Remove `proposal_uri` field from the model; clients can construct URIs on-demand from IDs.
> Benefits: single source of truth, smaller payloads, no sync risk, consistency with ID-based
> patterns.

```
     175:
     176:     id: str
     177:     status: ProposalStatus
>>>  178:     created_at: datetime
     179:     decided_at: datetime | None = None
     180:     proposal_uri: str  # URI to access full proposal content in policy server
     181:
```

## ducktape/2025-11-22-01 (2)

### `duplicate-ts-types.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-01/issues/duplicate-ts-types.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-01/code/adgn/src/adgn/agent/web/src/features/chat/channels.ts#L138-L174)

File: `adgn/src/adgn/agent/web/src/features/chat/channels.ts` (L138-174)

> The `channels.ts` file (lines 138-174) manually defines TypeScript types for
> WebSocket messages (SessionMessage, McpMessage, ApprovalsMessage, etc.).
>
> The codebase already has a Pydantic-to-TypeScript code generator at
> `adgn/scripts/generate_frontend_code.py` that uses `json-schema-to-typescript`,
> outputs to `adgn/agent/web/src/generated/types.ts`, and is invoked via
> `npm run codegen`.
>
> Manual types create duplication, drift risk (Python changes may not reflect in
> TypeScript), and maintenance burden (schema changes require two updates).
>
> **Fix:** Find or create Python Pydantic models for SessionMessage, McpMessage,
> ApprovalsMessage, PolicyMessage, UiMessage, ErrorMessage (likely in
> `adgn/agent/server/protocol.py`). Add them to `models_to_export` in
> `generate_frontend_code.py`. Run `npm run codegen`. Replace manual types in
> channels.ts with imports from `generated/types.ts`. Keep only envelope type
> manually defined (infrastructure, not data model).

```
     135:  * Channel message type guards
     136:  */
     137:
>>>  138: export type SessionMessage =
>>>  139:   | { type: 'session_snapshot'; session_state: any; run_state?: any }
>>>  140:   | { type: 'user_text'; text: string }
>>>  141:   | { type: 'assistant_text'; text: string }
>>>  142:   | { type: 'tool_call'; name: string; args_json?: string; call_id: string }
>>>  143:   | { type: 'tool_result'; call_id: string; output: string; is_error?: boolean }
>>>  144:   | { type: 'reasoning'; text: string }
>>>  145:   | { type: 'run_status'; run_state: any }
>>>  146:   | { type: 'turn_done' }
>>>  147:
>>>  148: export type McpMessage =
>>>  149:   | { type: 'mcp_snapshot'; sampling: any }
>>>  150:   | { type: 'mcp_server_attached'; name: string }
>>>  151:   | { type: 'mcp_server_detached'; name: string }
>>>  152:
>>>  153: export type ApprovalsMessage =
>>>  154:   | { type: 'approvals_snapshot'; pending: any[] }
>>>  155:   | { type: 'approval_pending'; call_id: string; tool_key: string; args_json?: string }
>>>  156:   | { type: 'approval_decision'; call_id: string; decision: string }
>>>  157:
>>>  158: export type PolicyMessage =
>>>  159:   | { type: 'policy_snapshot'; policy: any }
>>>  160:   | { type: 'policy_updated'; version: number }
>>>  161:   | { type: 'policy_proposal'; proposal: any }
>>>  162:
>>>  163: export type UiMessage =
>>>  164:   | { type: 'ui_state_snapshot'; v: string; seq: number; state: any }
>>>  165:   | { type: 'ui_state_updated'; v: string; seq: number; state: any }
>>>  166:   | { type: 'ui_message'; message: any }
>>>  167:   | { type: 'ui_end_turn' }
>>>  168:
>>>  169: export type ErrorMessage = {
>>>  170:   type: 'error'
>>>  171:   code: string
>>>  172:   message?: string
>>>  173:   details?: any
>>>  174: }
     175:
     176: export type AcceptedMessage = {
     177:   type: 'accepted'
```

### `redundant-args-json-param.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-01/issues/redundant-args-json-param.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/ducktape/2025-11-22-01/code/adgn/src/adgn/agent/agent.py#L261-L334)

File: `adgn/src/adgn/agent/agent.py` (L261-265, L276-280, L305, L334)

> Lines 261-265 define `_invoke()` with parameters `function_call: FunctionCallItem` and
> `args_json: str | None`. Call sites (lines 305, 334) always pass `args_json` as
> `function_call.arguments`, making it redundant. Lines 276-280 parse `args_json` but could
> use `function_call.arguments` directly.
>
> This creates data duplication (same data passed twice), cognitive load (reader must verify
> args_json matches function_call.arguments), and potential inconsistency (nothing enforces
> equality). Arguments already accessible via function_call object.
>
> Remove `args_json` parameter from `_invoke()` signature (lines 261-265). Replace `if args_json:`
> check (line 277) with `if function_call.arguments:`. Update call sites (lines 305, 334) to pass
> only `function_call`. Establishes single source of truth, simpler signature, and type safety.

```
     258:             evt.call_id: evt.result for evt in self._transcript if isinstance(evt, ToolCallOutput)
     259:         }
     260:
>>>  261:         async def _invoke(
>>>  262:             function_call: FunctionCallItem,
>>>  263:             args_json: str | None,
>>>  264:             local_map: dict[str, CallToolResult] = local_result_map,
>>>  265:         ) -> ToolCallOutcome:
     266:             cid = _require_call_id(function_call)
     267:             # No agent-level before-tool gating; Policy Gateway middleware enforces approvals/denials
     268:             if cid in local_map:
   ...
     273:             # Invoke via Policy Gateway client; do not swallow exceptions.
     274:             # Parse arguments strictly; invalid JSON/object shape is a hard error.
     275:             args: dict[str, Any] = {}
>>>  276:             if args_json:
>>>  277:                 val = json.loads(args_json)
>>>  278:                 if not isinstance(val, dict):
>>>  279:                     raise ValueError("tool arguments must be a JSON object")
>>>  280:                 args = val
     281:             raw = await self._mcp_client.call_tool(function_call.name, args, raise_on_error=False)
     282:             res = copy.deepcopy(raw)
     283:             if res.is_error:
   ...
     302:             async def runner(fc: FunctionCallItem) -> None:
     303:                 nonlocal abort_triggered
     304:                 try:
>>>  305:                     outcome = await invoker(fc, fc.arguments)
     306:                 except cancelled_exc:
     307:                     return
     308:                 cid = _require_call_id(fc)
   ...
     331:         self, function_calls: list[FunctionCallItem], invoker
     332:     ) -> None:
     333:         for i, function_call in enumerate(function_calls):
>>>  334:             outcome = await invoker(function_call, function_call.arguments)
     335:             self._emit_tool_result(function_call, outcome.result)
     336:             if isinstance(outcome, ToolCallAborted):
     337:                 for remaining in function_calls[i + 1 :]:
```

## gmail-archiver/2025-12-17-00 (1)

### `naive-aware-comparison.yaml` / `occ-0`

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/gmail-archiver/2025-12-17-00/issues/naive-aware-comparison.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/gmail-archiver/2025-12-17-00/code/gmail_archiver/planners/aliexpress.py#L112-L199)

File: `gmail_archiver/planners/aliexpress.py` (L112, L131, L181, L199)

> `compute_deadline()` returns naive datetimes from both code paths:
> `extract_delivered_date` returns `datetime(year, month, day)` (line 112,
> no tzinfo), and the fallback explicitly strips timezone with
> `.replace(tzinfo=None)` (line 131). But `AliExpressPlanner.plan()` creates
> `now = datetime.now(UTC)` (line 181, timezone-aware) and then compares
> `full_parsed.confirmation_deadline < now` (line 199). Comparing a naive
> datetime against an aware datetime raises `TypeError: can't compare
offset-naive and offset-aware datetimes`. Fix by making
> `compute_deadline()` return aware datetimes. The delivered date is a bare
> calendar date ("Delivered DD/MM/YYYY") with no intrinsic timezone — what
> zone to assign is somewhat ambiguous (user-local would be most precise
> for "has this day passed," but UTC is fine given the 15-day window).

```
     109:     if match := DELIVERED_DATE_PATTERN.search(body):
     110:         day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
     111:         try:
>>>  112:             return datetime(year, month, day)
     113:         except ValueError:
     114:             return None
     115:     return None
   ...
     128:     # Fallback: email received date + 30 days
     129:     try:
     130:         received = parsedate_to_datetime(message.date)
>>>  131:         received = received.replace(tzinfo=None)
     132:         return received + timedelta(days=30)
     133:     except (ValueError, TypeError):
     134:         return None
   ...
     178:             except AliExpressParseError as e:
     179:                 unparseable.append((msg, str(e)))
     180:
>>>  181:         now = datetime.now(UTC)
     182:
     183:         # Report unparseable emails but don't take action
     184:         if unparseable:
   ...
     196:             if latest_parsed.status in STATES_WITH_DEADLINE:
     197:                 full_parsed = parse_aliexpress(latest, should_compute_deadline=True)
     198:
>>>  199:                 if full_parsed.confirmation_deadline and full_parsed.confirmation_deadline < now:
     200:                     # Deadline passed - archive all emails for this order
     201:                     for msg in sorted_emails:
     202:                         plan.add_action(
```
