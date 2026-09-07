# LangGraph (removed)

Implemented the Twenty Questions guesser/simulator game as a LangGraph `StateGraph` —
`guesser`/`simulator`/`exec` nodes wired by conditional edges — plus a separate
lower-level, non-graph implementation (`run_twenty_questions_langgraph`) that drove the
same roles as a plain loop of `ainvoke()` calls without the graph machinery. Removed
2026-09-07: `langchain-mcp-adapters` (latest checked: 0.3.2) requires `mcp<2.0.0`
(tracked upstream at `langchain-ai/langchain-mcp-adapters#578`), with no release
compatible with mcp-sdk >=2.0 — see issue #5786. Full source is in git history
(`git log --diff-filter=D -- 'skills/info_gathering/evals/twenty_questions/x/langgraph/*'`);
this page keeps only what's worth knowing without digging it back up: how its MCP tool
integration was shaped, and why it looked the way it did.

## MCP integration: `langchain_mcp_adapters.load_mcp_tools`, no bridging needed

LangChain's tool protocol (`BaseTool.ainvoke`) is already async, and LangGraph's graph
nodes are already coroutines — so plugging in an MCP-backed tool needed no thread or event
loop juggling, just a converter from an open `fastmcp.client.Client` session to LangChain
`BaseTool` objects:

```python
async with contextlib.AsyncExitStack() as stack:
    game_tools: list[BaseTool] = _build_game_tools(game)

    if exec_server is not None:
        mcp_client = await stack.enter_async_context(Client(exec_server))
        tools = await load_mcp_tools(mcp_client.session)
        exec_tool = next(t for t in tools if t.name == "exec")
        game_tools.append(exec_tool)

    guesser_with_tools = guesser_model.bind_tools(game_tools, tool_choice="required")
```

`load_mcp_tools` reflects the server's `tools/list` and hands back ready-to-bind
`BaseTool`s directly off the live session — one line where CrewAI's equivalent
integration (<crewai.md>) needed a whole background-thread bridge, because CrewAI's tool
protocol is synchronous and MCP's client isn't.

## Guesser tools were hand-written LangChain tools, not MCP tools

Only `exec` came from MCP. The game's own two guesser actions
(`ask_yes_no_question`, `guess_answer`) were plain `StructuredTool.from_function(...)`
wrappers around closures that captured a mutable `GameContext` — the same "closure over
shared game state" shape every framework in this comparison used for its game-specific
tools, MCP or not:

```python
async def ask_yes_no_question(question: str) -> str:
    return await _ask_yes_no_question(game, question)

ask_tool = StructuredTool.from_function(
    coroutine=ask_yes_no_question,
    name="ask_yes_no_question",
    description="Ask a yes/no question about the secret.",
    args_schema=AskYesNoQuestionInput,
)
```

## Forcing a tool call every turn: `tool_choice="required"`

Both the guesser and the simulator needed to _always_ respond via a tool call — a text
reply from the simulator, for instance, has no defined meaning in the game protocol.
LangChain's `bind_tools(..., tool_choice="required")` was the mechanism; the simulator's
three-tool schema (`answer`/`correct_answer`/`invalid_input`) was bound the same way
whether the game used the graph or the plain-loop implementation.

## Two implementations of the same game, never reconciled

`build_graph` (a real `StateGraph`, exercised by the unit tests) and
`run_twenty_questions_langgraph` (the CLI's actual entry point, a hand-rolled
`for`-loop calling `ainvoke` directly) reimplemented the same guesser/simulator/exec
turn logic in parallel rather than one calling the other. That duplication was a known
rough edge in this variant, not a LangGraph integration lesson — worth naming here only
so it isn't rediscovered as news if this variant is ever restored.
