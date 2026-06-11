# 2026-04-28 gemma followup — partial / mostly-failed

Targeted re-run of `c-gemma31` after the initial pass left its rows blank
because the bench parser missed gemma's reasoning chunks. The bench was
patched in two ways:

1. Recognize `delta.reasoning` and `delta.thinking` as reasoning-chunk
   markers in addition to `delta.reasoning_content`. Gemma uses the
   `delta.reasoning` field name; gpt-oss uses `delta.reasoning_content`.
2. Add `REASONING_EFFORT="off"` mode that sends `think: false` (Ollama-
   native boolean) and omits `reasoning_effort` (no documented "off"
   value in the OpenAI spec).

This run was supposed to measure gemma with reasoning suppressed. It
didn't fully succeed.

## What ran

- Job: <job.yaml> (`MODELS=gemma4:31b-it-q8_0`, `REASONING_EFFORT=off`)
- Bench: <bench.py> (with the two patches above)
- Driver: <run.sh>
- Endpoint: `http://ollama.ollama:11434/v1/chat/completions`

## What happened

### Warmup ran but came back as 100% reasoning

```text
{"event": "warmup",
 "model": "gemma4:31b-it-q8_0",
 "t_first_reasoning": 274.5,
 "t_first_content": null,
 "t_done": 281.4,
 "completion_tokens": 256,
 "reasoning_chunks": 227,
 "content_chunks": 0,
 ...}
```

So `think: false` in the OpenAI-compat body did **not** disable gemma's
reasoning. Gemma reasoned for the full 256-token budget and never
emitted a content chunk. (The new parser at least correctly classified
all 256 tokens as reasoning instead of dropping them on the floor — that
part of the patch worked.)

### First trial wedged

Bench moved to trial 1 (1024-token-target prompt). Ollama's server logs
showed gemma get re-loaded at the right time; `nvidia-smi` showed
GPU 0 cycling 4–24% utilization (decode in progress); but no chunks
ever made it back to the bench, no `trial` event was emitted, and
ollama logged no chat-completions request completing for ~3 min before
we killed it. Suspect either a streaming-buffer pathology specific to
gemma's reasoning output or a Cilium/keep-alive socket issue. Did not
investigate further — gemma-on-Ollama isn't a top destination.

## Findings worth keeping

1. **Gemma streaming uses different field names than gpt-oss.** OpenAI
   compat: gpt-oss → `delta.reasoning_content`, gemma → `delta.reasoning`.
   Ollama native: both → `message.thinking`. Bench parser must be
   liberal in what it accepts.
2. **`think: false` on `/v1/chat/completions` is silently ignored** for
   gemma (and probably for any model where Ollama's OpenAI shim doesn't
   forward the field). To actually disable thinking, hit `/api/chat`
   (Ollama native), where `think: false` is documented and honored.
   We're not going to fix this now — gemma rows in `benchmarks.md` stay
   marked as "n/a" with a footnote pointing here.
3. **Gemma also has minor CPU offload** on this hardware. Ollama load
   logs show `model weights device=CPU size=1.5 GiB` alongside ~15.4 GiB
   per GPU, so ~5% of weights overflow. With 262K context allocated by
   default and Q8_0 KV, total memory hits 51 GiB. Much smaller spillover
   than `gpt-oss:120b` (19%); not a perf concern, but worth knowing.
4. **First-trial-after-warmup hang reproduced once**, may or may not be
   gemma-specific. If it bites again on another model, file a real
   investigation; not now.

## Why we're not chasing the bench fix

For throughput measurement, reasoning vs content tokens decode at the
same rate, so the per-phase split isn't load-bearing. Combined-decode
TPS (= `completion_tokens / (t_done - t_first_event)`) would still be
useful, but the trial hang prevented us from getting it.

The cleanest next step for gemma throughput is the same as for the
other Ollama configs: rerun with a single endpoint that we trust
(probably `/api/chat`), accept gemma reasons by default, and report
combined-decode TPS. Deferred — see the proposed-suite next pass in
<../../benchmarks.md#proposed-benchmark-suite>.

## Raw data

- <raw_output.jsonl> — warmup event only; trial 1 never completed.
