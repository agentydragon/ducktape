# Info-Gathering Skill: Evaluation Cases

Each eval tests a different aspect of the info-gathering skill. See each eval's
`README.md` for details on variants, running, and evaluation criteria.

## Evals

| Directory           | What it tests                                   |
| ------------------- | ----------------------------------------------- |
| `twenty_questions/` | Convergence on a fixed domain via binary search |

## Harness

`harness.py` provides shared infrastructure:

- `model_from_args` — OpenAI Responses API model construction with retries
- CLI utilities (`add_common_args`, `output_dir_from_args`, etc.)
- Result models (`RunSummary`, `LogEntry`)

`docker_scratch.py` provides an ephemeral Docker container as a `ToolProvider`
for agent scratch computation.
