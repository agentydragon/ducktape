# Info-Gathering Skill: Evaluation Cases

Each eval tests a different aspect of the info-gathering skill. See each eval's
`README.md` for details on variants, running, and evaluation criteria.

## Evals

| Directory           | What it tests                                   |
| ------------------- | ----------------------------------------------- |
| `twenty_questions/` | Convergence on a fixed domain via binary search |

## Harness

`harness.py` provides shared infrastructure:

- `LLMClient` — LiteLLM wrapper (call, resolve_tool_calls)
- CLI utilities (`add_common_args`, `client_from_args`, etc.)
- Result models (`RunSummary`, `LogEntry`, `TokenTracker`)

`litellm_tool_provider.py` handles LiteLLM ↔ `ToolProvider` wiring
(`tool_params_from_provider`, `tool_result_content`).

`docker_scratch.py` provides an ephemeral Docker container as a `ToolProvider`
for agent scratch computation.
