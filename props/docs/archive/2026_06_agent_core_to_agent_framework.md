# Replacing `agent_core` with a standard agent interface

> **Archived — the evaluation was carried out.** props production agents run on
> Microsoft Agent Framework today: `props/agents/af/` holds the client, loop,
> middleware, tools, and exec tool (`client.py` states outright that it replaces
> `agent_core`'s `create_bound_model_from_env`), and `props/agents/critic/main.py`
> imports from it. `agent-framework-{core,openai,anthropic}` are pinned in
> `pyproject.toml`. `agent_core` survives in props only as a test-mock library
> (`//agent_core/testing:responses`) and outside props under `x/`. Kept for the
> comparison of alternatives and the middleware-mapping reasoning; it is not a
> current plan.

**Status:** evaluation / design note, written before the migration
**Date:** 2026-06-07

## Goal

`agent_core` is our in-house agent loop. The question: can props move off it onto a
standard, less-custom interface — primarily **Microsoft Agent Framework (MAF)**, the
`agent-framework` Python package (1.0 GA April 2026) — and what's easy vs. hard?

Short answer: the tool layer and model layer port almost directly. The control loop —
which is where props put all its bespoke behavior — also maps well, because MAF 1.0 exposes
a **three-layer middleware** model (agent / function / **chat**), and chat middleware runs
**once per model call inside the tool-calling loop**. There's no hard blocker: the behaviors
that don't reduce to middleware (notably "keep going after the model emits a plain text
answer", reminder-on-text) reduce to a plain outer `while` loop over `agent.run()` on a
persistent thread — arguably cleaner than `agent_core`'s in-loop handler injection.

## Scope / blast radius

`agent_core` has two consumers: **props** and `x/editor_agent` (`x/editor_agent/host/agent_runner.py`).
Within props, the live (non-test, non-specimen) surface is small and concentrated — **7 files**:

| File                                                      | What it uses                                                                                                            |
| --------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `props/agents/critic/main.py`                             | `Agent`, `DirectToolProvider`, `RedirectOnTextMessageHandler`, `AbortIf`, `LoggingHandler`, `AllowAnyToolOrTextMessage` |
| `props/agents/critic_dev/main.py` + `loop.py`             | same + custom `BaseHandler` returning `InjectItems`/`Abort`                                                             |
| `props/agents/grader/main.py` + `notification_handler.py` | same + custom `BaseHandler` injecting async `pg_notify` messages via `InjectItems`                                      |
| `props/agents/runtime.py`                                 | model binding (`create_bound_model_from_env`), prompt rendering                                                         |
| `props/core/gepa/gepa_adapter.py`                         | drives critic/grader runs                                                                                               |

Plus ~14 test files using `agent_core.testing` (mock model / fixtures). The
`props/specimens/**` hits are frozen snapshot copies of the old `adgn` codebase — not live.

## What props actually uses from `agent_core`

The three live agents share one shape:

```python
agent = await Agent.create(
    tool_provider=DirectToolProvider(...),   # @provider.tool python fns, Pydantic args in, str/Pydantic out
    handlers=[LoggingHandler(...), RedirectOnTextMessageHandler(REMINDER), AbortIf(lambda: exit_state.should_exit)],
    client=bound_model,                       # create_bound_model_from_env: chat-completions adapter + LLM proxy + budget
    parallel_tool_calls=False,
    tool_policy=AllowAnyToolOrTextMessage(),
)
agent.process_message(SystemMessage.text(system_prompt))
await agent.run()
```

The load-bearing abstraction is the **handler / `on_before_sample` → `LoopDecision`** hook.
Each handler observes events (`on_tool_call_event`, `on_assistant_text_event`, …) and, before
each model sample, returns a decision: `NoAction`, `InjectItems(items, tool_policy)`, `Abort`,
or `Compact`. All of props' control logic lives there:

- **Reminder-on-text** (`RedirectOnTextMessageHandler`): model emitted prose instead of a tool
  call → inject a reminder and force tools next turn.
- **Terminate-on-tool** (`AbortIf` + the `submit` / `report_failure` tools flipping `exit_state`):
  end the loop when a specific tool fires.
- **Async out-of-band injection** (grader): drain pending `pg_notify` and inject as a batched
  `UserMessage` before the next sample.
- **Dynamic tool policy** (`ToolPolicy` = `AllowAnyToolOrTextMessage` / `RequireAnyTool` /
  `ForbidAllTools` / `RequireSpecific`) → per-turn `tool_choice`.
- Plus observability (`LoggingHandler` / `TranscriptHandler`), turn caps (`MaxTurnsHandler`),
  and context compaction (`CompactionHandler`, threshold + keep-recent-N).

## MAF current surface (verified, Python 1.0, April 2026)

Relevant primitives:

- **Agent + chat client**: `Agent(client=<ChatClient>, instructions=…, tools=[…], middleware=[…])`,
  `await agent.run(messages)`. Chat clients: `OpenAIChatClient`, `AzureOpenAIChatClient`,
  `FoundryChatClient`, etc.
- **Tools**: plain Python callables decorated with `@tool`, params typed via
  `Annotated[T, Field(description=…)]` (auto JSON-schema). MCP tools via dedicated tool classes.
- **Three middleware layers** (function-based, class-based, or `@agent_middleware` / `@function_middleware`
  / `@chat_middleware` decorators), each mutating a shared context then awaiting `call_next()`:
  - **Agent middleware** — `AgentContext`: `messages` (mutable), `result` (mutable), `options`,
    `session`, `metadata`, `function_invocation_kwargs`. Wraps the whole run.
  - **Function middleware** — `FunctionInvocationContext`: `function`, `arguments`, `result`
    (mutable). Wraps each tool call. Can **terminate the loop** (`.Terminate` / `MiddlewareTermination`).
  - **Chat middleware** — `ChatContext`: `messages` (mutable), `options` (mutable, incl. `tool_choice`),
    `result` (mutable). **Runs for _each_ model call inside the tool-calling loop**, including the
    calls that send tool results back.
- **Termination / override**: any middleware can set `context.result` and raise `MiddlewareTermination`,
  or just not call `call_next()`.
- **Compaction**: experimental `agent_framework._compaction` — `ToolResultCompactionStrategy`
  (collapse all but the newest tool-call group) and an LLM **summarization** strategy, surfaced as a
  `CompactionProvider` (an `AIContextProvider`).
- Built-in OpenTelemetry tracing.

## Feature-by-feature mapping

| `agent_core` feature                                                                                                                | MAF idiom                                                                             | Difficulty                                                                           |
| ----------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `DirectToolProvider` python tools                                                                                                   | `@tool` callables (`Annotated` + `Field`)                                             | 🟢 direct (reshape single-arg-model → per-param annotations, or keep a Pydantic arg) |
| `MCPToolProvider`                                                                                                                   | MAF MCP tool classes                                                                  | 🟢 direct                                                                            |
| `bound_model` (chat-completions + proxy + budget)                                                                                   | `OpenAIChatClient(base_url=…)` + a thin **chat middleware** for budget/proxy          | 🟢🟡 direct client; budget/proxy = small custom middleware                           |
| static `tool_policy`                                                                                                                | `ChatOptions.tool_choice`                                                             | 🟢 direct                                                                            |
| dynamic `ToolPolicy` per turn                                                                                                       | **chat middleware** mutates `context.options.tool_choice` per model call              | 🟢 direct                                                                            |
| `InjectItems` (reminders, async notifications)                                                                                      | **chat middleware** mutates `context.messages` before the call                        | 🟢 direct                                                                            |
| terminate-on-tool (`AbortIf` + `submit`/`report_failure`)                                                                           | **function middleware** raises `MiddlewareTermination` after the terminal tool runs   | 🟢 direct (terminate _after_ the tool returns to keep history consistent)            |
| `LoggingHandler` / `TranscriptHandler`                                                                                              | any middleware layer + built-in OTel                                                  | 🟢 direct                                                                            |
| `MaxTurnsHandler`                                                                                                                   | max tool-iteration cap / counter in agent middleware                                  | 🟢 direct                                                                            |
| `CompactionHandler` (threshold + keep-N)                                                                                            | `agent_framework._compaction` (`ToolResultCompactionStrategy` / summarization)        | 🟡 maps, but **experimental**; semantics differ from ours                            |
| strict-schema enforcement, tool-result size cap, sanitization (`OpenAIStrictModeBaseModel`, `_check_size`, `_sanitize_tool_result`) | **function middleware** (validate args, cap/sanitize `result`)                        | 🟡 reimplement, but a known shape                                                    |
| event/content model (`events.py`, `ToolResult`, `AssistantText`, …)                                                                 | MAF `Message` / `Content` / `FunctionInvocationContext` / `AgentResponse`             | 🟡 mechanical re-typing of the 7 prod files + custom handlers                        |
| `agent_core.testing` mocks (14 test files)                                                                                          | MAF mock/test chat client                                                             | 🟡 contained but volume                                                              |
| **reminder-on-text** (continue after a plain text answer)                                                                           | outer `while` loop re-invoking `agent.run(reminder, thread=…)` on a persistent thread | 🟢 plain Python loop (MAF threads persist history across `run()` calls)              |

## Reminder-on-text is just an outer loop

`RedirectOnTextMessageHandler` keeps going when the model answers in prose instead of calling a tool.
MAF's tool-calling loop **terminates** when the model returns no tool calls — that _is_ its natural end
state. But there's no need to keep the redirect _inside_ one `run()`: MAF persists conversation state
in an `AgentThread`/session across `run()` calls, so props just owns a plain outer loop:

```python
thread = agent.get_new_thread()
agent.run(system_prompt + task, thread=thread)
while not exit_state.should_exit:                 # set by the submit/report_failure tool
    result = await agent.run(thread=thread)       # continues the tool loop on the same history
    if result_is_bare_text(result):               # model answered in prose instead of a tool
        await agent.run(TEXT_OUTPUT_REMINDER, thread=thread)  # re-prompt; force tools via ChatOptions/chat middleware
```

This is actually _cleaner_ than `agent_core`'s in-loop handler injection: the continue/stop decision
is ordinary control flow in props, and MAF's `run()` handles each tool-calling burst. Terminate-on-tool
stays a function-middleware concern (the terminal tool sets `exit_state`; middleware ends that `run()`).
So none of props' control behaviors is a hard blocker — they're middleware shims plus this outer loop.

## Suggested migration shape

1. **Port `critic` first** — it's the simplest (no async injection, terminate-on-tool, one reminder).
   Prove the pattern: tools via `@tool`, an outer run loop for reminder-on-text, chat middleware to
   force `tool_choice`, function middleware for terminate-on-`submit`/`report_failure` + size caps,
   and a chat-middleware (or custom client) wrap for the proxy/budget.
2. **Adapter seam**: introduce a thin props-side interface (`build_agent(...) -> run()`), implement
   it on MAF, and keep the agents coded against the seam — so `grader`/`critic_dev`/`x/editor_agent`
   migrate independently and we can A/B against the `agent_core` implementation.
3. **`grader`** next (async `pg_notify` injection → chat middleware mutating `messages`).
4. **`critic_dev`** + compaction last (depends on `_compaction` maturity).
5. Replace `agent_core.testing` usage with a MAF mock chat client as each agent moves.

This also incidentally widens model options: MAF's chat-client ecosystem makes the **Anthropic-shaped
z.ai path** (the union-tool-input escape hatch from the GLM-4.6 work — see
<../../../docs/zai_api.md> and `agent_core/test_zai_chat_adapter_live.py`) easier to adopt than in
`agent_core`, which today only has OpenAI Responses + Chat Completions adapters.

## Open questions (resolve with a short spike before committing)

- Confirm an `AgentThread`/session **persists full history across separate `agent.run()` calls** so
  the outer reminder-on-text loop continues the same conversation (this underpins the migration shape).
- Confirm chat middleware can **mutate `context.options.tool_choice`** and **inject into
  `context.messages`** and have it take effect on that same model call (the docs strongly imply it;
  verify empirically).
- Confirm function-middleware **`Terminate` after the tool returns** leaves chat history consistent
  for our terminal tools.
- Validate `_compaction` (experimental) covers our "threshold + keep-recent-N" need, or whether we
  keep a custom compaction provider.
- Does MAF ship a first-class **Anthropic / non-OpenAI** chat client (for the z.ai Anthropic shape),
  or do we wrap one?
- `x/editor_agent` is the other `agent_core` consumer — decide whether it migrates in lockstep or
  `agent_core` lingers for it.

## Alternatives (if MAF doesn't fit)

- **OpenAI Agents SDK** — similar tools + handoffs + guardrails; loop also framework-owned.
- **Pydantic AI** — strong typed-tool ergonomics; lighter control-loop hooks.
- **Raw OpenAI/Anthropic SDK loop** — least magic; basically what `agent_core` is, minus the custom
  abstractions. Lowest dependency, highest in-house maintenance.

MAF is the best fit on paper because its per-model-call **chat middleware** + **function-middleware
termination** line up almost exactly with the `on_before_sample`/`LoopDecision` and `AbortIf` patterns
props already uses.

## Sources

- [Agent Framework overview](https://learn.microsoft.com/en-us/agent-framework/overview/)
- [Agent middleware](https://learn.microsoft.com/en-us/agent-framework/agents/middleware/) (agent / function / chat contexts, per-model-call chat middleware, `MiddlewareTermination`)
- [Compaction](https://learn.microsoft.com/en-us/agent-framework/agents/conversations/compaction) (`ToolResultCompactionStrategy`, summarization, `CompactionProvider`)
- [Python 2026 significant changes](https://learn.microsoft.com/en-us/agent-framework/support/upgrade/python-2026-significant-changes) (FunctionInvocation outermost; chat middleware per model call)
- [microsoft/agent-framework](https://github.com/microsoft/agent-framework)
