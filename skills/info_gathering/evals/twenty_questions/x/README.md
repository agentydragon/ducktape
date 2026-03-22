# Twenty Questions Framework Comparison

Reimplementation of the Twenty Questions eval game across 7 agent frameworks to compare developer experience and ergonomics.

## The Game

Two LLM agents alternate turns:

- **Guesser**: Asks yes/no questions (has the info-gathering skill system prompt)
- **Simulator**: Holds a secret, responds via tool calls only (`answer` or `correct_answer`)

**Variants**: `states` (secret="New Mexico", limit=20), `wide` (secret="a sourdough starter", limit=25)

## Frameworks

| Framework         | Language | Directory        | Status      |
| ----------------- | -------- | ---------------- | ----------- |
| PydanticAI        | Python   | `pydantic_ai/`   | Implemented |
| LangGraph         | Python   | `langgraph/`     | Implemented |
| AutoGen/AG2       | Python   | `autogen/`       | Implemented |
| CrewAI            | Python   | `crewai/`        | Implemented |
| OpenAI Agents SDK | Python   | `openai_agents/` | Implemented |
| Rig               | Rust     | `rig/`           | Implemented |
| Genkit            | Go       | `genkit/`        | Implemented |

## Usage

```bash
# Run a specific implementation
bazel run //skills/info_gathering/evals/twenty_questions/x/pydantic_ai:twenty_questions_bin -- \
  --variant states --model gpt-4o-mini --api openai

# Compare results
bazel run //skills/info_gathering/evals/twenty_questions/x:compare_results_bin -- \
  --results-dir /tmp/twenty_questions_results
```

## Shared Infrastructure

- `shared/variants.py` — Variant definitions
- `shared/result_types.py` — Pydantic models for results
- `shared/output.py` — File output helpers
- `shared/cli.py` — Common CLI argument parsing
- `shared/prompts.py` — Prompt loading (references original files via Bazel runfiles)
- `shared/docker_exec.py` — Lightweight Docker scratch container

Prompts are not duplicated — they reference the originals at:

- `//skills/info_gathering:SKILL.md`
- `//skills/info_gathering/evals/twenty_questions:sim.txt`
