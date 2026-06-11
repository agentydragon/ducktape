# Aborted run: react agent vs Ollama `num_ctx=262144` rejection

Started 2026-04-30 00:31:53 PDT, killed at +1h05m. **0/100 samples
completed** — every chat-completion request to Ollama returned 500.

## What we observed

- 148 `POST /v1/chat/completions` requests in 2 h, 103 of them 500
  (the rest were the GET healthchecks; only 1 unrelated 200 was my
  ad-hoc smoke-test from outside the eval).
- Both running samples got stuck in inspect-ai's exponential-backoff
  retry loop (retry 10 → retry 11, backoffs of 1500–1800 s). Neither
  produced a single assistant turn.
- Containers were up, eval was registered (`status=started`), no
  `.eval` samples were written.

## Root cause

Ollama logs (`ollama_logs_during_run.txt`):

```text
WARN source=server.go:169 msg="requested context size too large for model"
     num_ctx=262144 n_ctx_train=131072
```

The model (`gpt-oss:20b`) is trained for 131 072 tokens (128 K).
Ollama's OpenAI-compatibility shim sizes the KV cache as
`prompt_tokens + max_tokens`, rounded up to the next power-of-two
block. SWE-bench prompts are large and Inspect AI did not specify a
`max_tokens` cap, so the round-up landed on **262 144** — over the
trained limit, and Ollama rejected the request as a 500.

This wasn't a problem on the AIME or HumanEval runs: those prompts
are short (< 4 K tokens) and Inspect's auto-sizing landed inside the
window.

## Fix for the next attempt

Pass `--max-tokens 8192` (or similar) on the `inspect eval`
invocation in `run_swebench.py`. The SWE-bench react agent's
generations are short (~1–3 K tokens of analysis + a tool call
or submit), so 8 K headroom is plenty. With `prompt_tokens` capped
implicitly by the eval's message turn-over and `max_tokens=8192`,
Ollama should size the KV cache to the next power-of-two block
≤131 072.

## Files

- `raw_output.txt` — Inspect's stdout/stderr (3.6 KB; mostly the
  splash plus 4 retry warnings). Renamed from `run.log` because
  `*.log` is gitignored under `cluster/`.
- `eval_logs/*.eval` — the in-flight log zip (`status=started`, no
  samples).
- `ollama_logs_during_run.txt` — `kubectl logs` over the run window;
  search for `num_ctx=262144` to see the 5 model-reload events that
  each emitted the warning.
