# 20 Questions Eval

Tests convergence of the info-gathering skill on a fixed domain.

## Variants

| Variant  | Domain                                                    | Secret              |
| -------- | --------------------------------------------------------- | ------------------- |
| `states` | US state (50 options, theoretical optimum ~5.6 questions) | New Mexico          |
| `wide`   | Any thing — object, place, concept, activity              | a sourdough starter |

## Running

```bash
# gpt-oss:20b through the default base URL (https://ollama.allegedly.works/v1)
bazel run //skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- \
  --variant states

# Another OpenAI-compatible Responses API endpoint, here cluster LiteLLM with a key from the k8s secret
OLLAMA_KEY=$(kubectl get secret ollama-api-key -n claude-sandbox \
  -o jsonpath='{.data.api-key}' | base64 -d) \
bazel run //skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- \
  --variant states \
  --model <route> \
  --base-url https://litellm.allegedly.works/v1 \
  --api-key "$OLLAMA_KEY"
```

Results go to `--output-dir` (default `eval_results/<timestamp>/`) as `<name>_<timestamp>_{calls.jsonl,summary.json}`.

## Evaluation criteria

- **Outcome**: Questions to convergence. Target ≤8, good ≤6 for `states`.
- **Process**:
  - Maintains a hypothesis space / entropy estimate
  - Questions approximately bisect remaining space
  - Avoids premature guessing (anchoring)
  - Does CHALLENGE (considers alternatives before final guess)
  - Uses scratch container for notes/computation
