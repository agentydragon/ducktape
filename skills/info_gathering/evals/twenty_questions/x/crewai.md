# CrewAI (removed)

Implemented the Twenty Questions guesser/simulator game as two CrewAI `Agent`s, each
driven one `Task` at a time through a fresh single-agent `Crew` (`Process.sequential`).
Removed 2026-09-07: `crewai` (latest checked: 1.15.20) pins `mcp~=1.28.1`, with no release
compatible with mcp-sdk >=2.0 — see issue #5786. Full source is in git history
(`git log --diff-filter=D -- 'skills/info_gathering/evals/twenty_questions/x/crewai/*'`);
this page keeps only what's worth knowing without digging it back up: how its MCP tool
integration was shaped, and why it looked the way it did.

## Simulator and guesser were both `Crew.kickoff()` calls

CrewAI's unit of execution is a `Crew` running one or more `Task`s. There's no
lower-level "just call the model with tools" entry point, so both game roles ran through
the same machinery even though only the guesser needed multi-turn tool access:

```python
def _run_guesser_turn(*, guesser: Agent, prompt: str) -> str:
    task = Task(
        description=prompt,
        expected_output="Use your tools to ask a yes/no question or guess the answer.",
        agent=guesser,
    )
    crew = Crew(agents=[guesser], tasks=[task], process=Process.sequential, verbose=False)
    result = crew.kickoff()
    raw = getattr(result, "raw", None)
    return str(raw).strip() if raw is not None else str(result).strip()
```

The simulator's three tools (`answer`, `correct_answer`, `invalid_input`) wrote into a
mutable `SimulatorToolState` object each `_run()` closed over, since a `Task`'s only
return channel is its final text — there's no structured way to read back _which_ tool a
single-task `Crew` run invoked other than a side channel like this.

## MCP integration: a background thread, because CrewAI tools are synchronous

The one MCP-backed tool (`exec`, a scratch Docker container reachable through
`mcp_infra.exec`) is where the CrewAI-specific cost showed up. `crewai.tools.BaseTool._run`
is a plain synchronous method, but `fastmcp.client.Client.call_tool` is a coroutine — and
the client has to stay bound to one `asyncio` event loop for its connection's lifetime.
That forced running the MCP client on a dedicated background thread with its own event
loop, and bridging every tool call across the thread boundary:

```python
class ExecTool(BaseTool):
    _mcp_client: Any = PrivateAttr()
    _loop: Any = PrivateAttr()

    def _run(self, cmd: list[str], cwd: str | None = None, timeout_ms: int = 30000) -> str:
        arguments: dict[str, Any] = {"cmd": cmd, "timeout_ms": timeout_ms}
        if cwd is not None:
            arguments["cwd"] = cwd
        future = asyncio.run_coroutine_threadsafe(self._mcp_client.call_tool("exec", arguments), self._loop)
        result = future.result()
        return "\n".join(block.text for block in result.content if hasattr(block, "text"))
```

The background loop itself was set up and torn down around the whole game run in
`_run_with_exec`: a `threading.Event` signaled once the MCP server+client were live (so
the main thread wasn't guessing with a sleep), and an `asyncio.Event` set from the main
thread via `call_soon_threadsafe` told the background loop when to close the client and
stop. Contrast this with LangGraph (<langgraph.md>), whose graph nodes are natively
`async` — it needed none of this bridging, because `langchain_mcp_adapters.load_mcp_tools`
handed back tools that were already awaitable in-place.

## Everything else was ordinary CrewAI

Model selection went through LiteLLM-style strings (`crewai_model_name`: `"gpt-4o-mini"`
for OpenAI, `"anthropic/claude-sonnet-5"` for Anthropic — CrewAI's `Agent(llm=...)` takes
a bare model string, not a client object). Game-turn bookkeeping (`GameState`, the
question/answer/timeout loop) was identical in shape to every other framework in this
comparison and carried no CrewAI-specific integration lessons.
