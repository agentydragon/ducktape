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

GEPA receives rich feedback for each evaluation, including successful and timed_out cases:

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

**2b. Timed Out** (when critic timed out before submitting):

```
critic_output: {"tag": "timed_out"}
grader_output: null
score: 0.0
trajectory: includes all tool calls/events but no critique_payload
```

The reflection LLM sees the discriminated union (success or timed_out) and can learn from cases where the critic got stuck or looped.

## Key Types

- `Example`: Training example from database (snapshot_slug, scope, scope_hash) - ORM model from `db/examples.py`
- `CriticTrajectory`: Execution trace (transcript_id, events, critique_payload or None if failed)
- `CriticOutput`: Evaluation result (critic_output discriminated union, grader_output or None, critique_id or None)
- `ReflectionExample`: Feedback for reflection LLM (current_text, score, trajectory, critic_output, grader_output or None)
- `CriticAdapter`: GEPA adapter wrapping Agent + grader
