# 20 Questions Eval

Tests convergence of the info-gathering skill on a fixed domain.

## Variants

| Variant  | Domain                                                    | Secret              |
| -------- | --------------------------------------------------------- | ------------------- |
| `states` | US state (50 options, theoretical optimum ~5.6 questions) | New Mexico          |
| `wide`   | Any thing — object, place, concept, activity              | a sourdough starter |

## Running

```bash
# Haiku with thinking (default, requires ANTHROPIC_API_KEY)
ANTHROPIC_API_KEY=sk-ant-... \
bazel run //skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- \
  --variant states

# Haiku without thinking (faster/cheaper)
ANTHROPIC_API_KEY=sk-ant-... \
bazel run //skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- \
  --variant states --thinking-budget 0

# Cluster Ollama (no API key required from env; retrieve from k8s secret)
OLLAMA_KEY=$(kubectl get secret ollama-api-key -n claude-sandbox \
  -o jsonpath='{.data.api-key}' | base64 -d) \
bazel run //skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- \
  --variant states \
  --model openai/gpt-oss-20b-128k \
  --base-url https://litellm.allegedly.works/v1 \
  --api-key "$OLLAMA_KEY" \
  --thinking-budget 0
```

Results are saved to `eval_results/` as `<name>_<timestamp>_{summary.json,calls.jsonl}`.

## Evaluation criteria

- **Outcome**: Questions to convergence. Target ≤8, good ≤6 for `states`.
- **Process**:
  - Maintains a hypothesis space / entropy estimate
  - Questions approximately bisect remaining space
  - Avoids premature guessing (anchoring)
  - Does CHALLENGE (considers alternatives before final guess)
  - Uses scratch container for notes/computation
