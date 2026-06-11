# GEPA-based Prompt Optimization for Props Critic

Uses [gepa-ai/gepa](https://github.com/gepa-ai/gepa) for evolutionary optimization of the critic system prompt.

## What GEPA Provides

- **Evolutionary search**: Population-based optimization over prompt variants
- **Reflection**: LLM analyzes traces to propose targeted improvements
- **Pareto optimization**: Multi-objective optimization (recall + precision)
- **Efficient**: Outperforms RL with fewer rollouts

## CLI Usage

```bash
# Default: full-snapshot examples
props gepa --max-metric-calls 100
```

## Feedback

GEPA receives rich feedback for each evaluation, including successful runs, max_turns_exceeded, and context_length_exceeded cases:

**1. Execution Traces** (from `events` table):

```
CALL docker_run_command({"command": "ruff check src/"})
  → src/foo.py:42: E501 Line too long...
CALL critic_submit_upsert_issue({"issue_id": "line-too-long", ...})
```

**2a. Grader Analysis** (when critic succeeded - full `GradeSubmitInput`):

```
MISSED ISSUES:
  - dead-import: The critic didn't check for unused imports
  - missing-type-annotation: No type checking performed
FALSE POSITIVES TRIGGERED:
  - trivial-style-nit: Known FP, should be ignored
SUMMARY: The critic focused on runtime issues but neglected...
```

**2b. Max Turns Exceeded** (when critic ran out of turns before submitting):

```
critic_output: {"tag": "max_turns_exceeded", "max_turns": 100}
grader_output: null
score: 0.0
trajectory: includes all tool calls/events but no critique_payload
```

**2c. Context Length Exceeded** (when prompt was too long for the model):

```
critic_output: {"tag": "context_length_exceeded", "error_message": "Error code: 400 - ..."}
grader_output: null
score: 0.0
trajectory: empty or incomplete (failed before agent could run)
```

The reflection LLM sees the full discriminated union (success, max_turns_exceeded, or context_length_exceeded) and can learn from cases where the critic got stuck, looped, wasted turns, or had prompts that were too long.

## Key Types

- `Example`: Training example from database (snapshot_slug, scope, scope_hash) - ORM model from `db/examples.py`
- `CriticTrajectory`: Execution trace (transcript_id, events, critique_payload or None if max_turns)
- `CriticOutput`: Evaluation result (critic_output discriminated union, grader_output or None, critique_id or None)
- `ReflectionExample`: Feedback for reflection LLM (current_text, score, trajectory, critic_output, grader_output or None)
- `CriticAdapter`: GEPA adapter wrapping Agent + grader
