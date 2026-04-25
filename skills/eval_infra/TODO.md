# eval_infra — TODO

## Replace hand-rolled tool-call loops with `agent_framework.Agent`

Both <skills/reverse_engineer/evals/runs/agent_framework/re_rollout.py>
and <skills/info_gathering/evals/twenty_questions/x/agent_framework/twenty_questions.py>
hand-roll the tool-calling loop:

```python
for step in range(_MAX_STEPS):
    response = await client.get_response(history, options={"tool_choice": "required",
                                                            "allow_multiple_tool_calls": False})
    function_calls = [c for c in response.messages[0].contents if c.type == "function_call"]
    for fc in function_calls:
        result = await tool.invoke(arguments=json.loads(fc.arguments))
        ...
        history.append(...)
```

Microsoft Agent Framework runs this loop internally. `agent_framework.Agent`
takes `(client, instructions, tools, default_options, middleware)` and
`agent.run(messages, ...)` drives the loop, dispatching tools via the
chat client's `function_invocation_configuration` (`max_iterations`,
`max_function_calls`). Both rollouts can collapse to:

```python
agent = Agent(
    client=model_client,
    instructions=system_prompt,
    tools=[exec_tool, submit_tool],
    middleware=[transcript_middleware],
    default_options={"tool_choice": "required", "allow_multiple_tool_calls": False},
)
client.function_invocation_configuration["max_iterations"] = _MAX_STEPS
try:
    await asyncio.wait_for(agent.run(first_user_message), timeout=_WALL_TIMEOUT_SECONDS)
except (MiddlewareTermination, asyncio.TimeoutError):
    ...
```

What goes away in each rollout:

- `_extract_function_calls(response)` — defensive filter that's only
  reachable when AF's `tool_choice="required"` contract breaks.
- `_make_exec_tool` is a candidate for moving here as a shared helper, OR
  for replacement entirely with AF's built-in MCP support
  (`agent_framework._mcp`) — confirm whether the agent can be given an
  MCP server URL/Client directly so we don't need the FunctionTool bridge.
- The `for step in range(_MAX_STEPS)` loop body — replaced by `agent.run()`.
- Manual `Content.from_function_result` + `Message("tool", ...)` plumbing.
- `_build_model_client(api, model)` likely earns its keep as a shared
  helper here once a second consumer exists; until then it's a 5-line
  switch each consumer can inline.

Per-turn transcript writes move into a `FunctionInvocationContext`
middleware that records `(step, tool_name, args, result, duration_ms)`
to JSONL. Submit becomes a tool whose body raises `MiddlewareTermination`
(or sets a sentinel checked by middleware that then raises) — that's how
AF surfaces "stop the loop" semantically.

Twenty_questions's simulator (called from inside guesser tools) survives
unchanged: a guesser tool body can `await sim_agent.run(...)` directly.

### Scope

- Land <skills/reverse_engineer/evals/runs/agent_framework/re_rollout.py> first
  with the hand-rolled loop (matches the existing twenty_questions style).
- Then a follow-up PR that:
  - Refactors the agent_framework variant of twenty_questions
  - Refactors re_rollout
  - Drops `_extract_function_calls` and similar from both
  - Adds `skills/eval_infra/agent_loop.py` (or similar) only if a real
    abstraction emerges that's worth sharing — otherwise both rollouts use
    AF directly with no shared loop helper.
- The other twenty_questions framework variants (crewai, langgraph,
  openai_agents, pydantic_ai) hand-roll their own loops in framework-native
  style; whether to do similar cleanup there depends on what each
  framework's idiomatic agent driver looks like.
