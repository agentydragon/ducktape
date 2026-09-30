# Qwen3.8 IQ4_XS through cluster Ollama and LiteLLM

September 27, 2026, wyrm2, two RTX 5090s. This is serving acceptance and a small
throughput/context check, not a capability benchmark. All test requests were serial.

## Outcome

The SSD-backed IQ4_XS model serves through cluster Ollama and authenticated LiteLLM.
At 256K configured context, a 145,048-token prompt successfully retrieves the exact
marker from its beginning; a 1,024-token continuation on that history decodes at
29.07 tokens/s. Short 256K-window requests decode at 44.39–49.48 tokens/s with the
final 4/2 GiB fit margins. The earlier host IQ4 agent trajectory averaged 40.79
tokens/s under different prompts, runtime, cache state and 8/2 GiB margins: this
clears the severe-HDD-slowdown acceptance criterion, not a controlled speedup claim.

Both 128K and 256K LiteLLM backend routes passed text and structured tool-call checks
through Chat Completions, Responses (streaming) and Anthropic Messages: 24/24 passes.
The initial OpenAI-backed 128K route also passed 6/6 checks and a tool-result round
trip, returning exactly `turquoise` from the tool's answer. The final 128K profile
decodes at 50.21–60.01 tokens/s on the short throughput request.

The 4/2 margins are a tested working point, not a proven minimum. The requested 2/0
margins were faster on short requests but crashed on a long input. Full 262K input,
parallel slots and long-document reasoning quality remain untested. The marker test
uses highly repetitive filler and is not a substitute for an agent evaluation.
Capacity, precision tradeoffs, external quality evidence and the next experiments
are in [tradeoffs.md](tradeoffs.md).

## Deployment and runtime

[Storage #8246](https://github.com/agentydragon/ducktape/pull/8246) binds a static local
PV/PVC to the existing SSD IQ4 directory. The HDD registry and other models remain;
three content-addressed blob symlinks point to the SSD GGUF shards. The serving
pod's `/ssd-models` mount is read-only.

[Registration #8250](https://github.com/agentydragon/ducktape/pull/8250) gives only a
short-lived Job write access because `/api/create` touches blob mtimes. Both model
registrations succeeded and its native sidecar exited with the completed Job. The
256K alias shares all blobs and sets its context default to 262,144, making that
window effective through the OpenAI wire too.

Ollama 0.34.4; bundled llama-server 0.4.1-dev, build 1, commit `161755f29`. Q8 K/V,
flash attention, one slot, six CPU threads, 40 GiB pod RAM limit. Actual runner uses
mmap and lazy lookup embeddings, with no MTP/draft arguments. This is hybrid
GPU/CPU/SSD serving despite the `offloaded 49/49 layers` log line.

[#8255](https://github.com/agentydragon/ducktape/pull/8255) sets fit targets to
4,096/2,048 MiB for runtime allocation room and extra desktop headroom. At 256K,
GPU weight buffers are 22,493.09 / 24,964.87 MiB; CPU-mapped buffers are
644.14 + 15,369.57 + 27,465.95 MiB. Mapped size is not resident host RAM. During
the long-input test and continuation, sampled free VRAM never fell below
3,919 / 1,812 MiB (five-second samples), and cgroup memory peak was 24.36 GiB with
zero host-memory OOM events. These fit targets are not exclusive reservations.

[#8252](https://github.com/agentydragon/ducktape/pull/8252) explicitly selects CUDA
and PCI bus ordering. Its original ordering-bug hypothesis was **not established**:
Ollama's startup log sorts a copy by free memory. The actual runner was verified as
`CUDA_VISIBLE_DEVICES=0,1`; do not infer runner order from that log.

## Measured requests

All short throughput requests use the committed `perf.request.json`: a compiler
explanation, thinking disabled, seed 42, temperature 0.6 and 1,024 output tokens.
Repeating the same prefix/output deliberately measures a warm case. Native counters
are retained in `initial-metrics.json` and `final-metrics.json`; `api-results.json`
retains the final API matrix results.

| Fit targets | Context setting | Input/history                           | First decode | Repeat decode | Result                                  |
| ----------- | --------------- | --------------------------------------- | -----------: | ------------: | --------------------------------------- |
| 2/0 GiB     | 131,072         | 51 tokens                               |  55.74 tok/s |   71.89 tok/s | Short requests pass                     |
| 2/0 GiB     | 262,144         | 51 tokens                               |  49.66 tok/s |   60.82 tok/s | Short requests pass; long input crashes |
| 4/2 GiB     | 131,072         | 51 tokens                               |  50.21 tok/s |   60.01 tok/s | Short requests pass                     |
| 4/2 GiB     | 262,144         | 51 tokens                               |  44.39 tok/s |   49.48 tok/s | Short requests pass                     |
| 4/2 GiB     | 262,144         | 145,110 tokens including cached history |  29.07 tok/s |             — | 1,024-token continuation passes         |

Load-only requests took 103.98 s (initial 128K), 74.06 s (initial 256K), and
91.85 s (4/2-margin 256K), and 71.93 s (4/2-margin 128K). These loads have different filesystem cache states.
Initial 128K prefill took 37.41 s, then 84.55 ms on repeat; initial 256K took
2.477 s, then 95.294 ms. The 4/2 short 256K prefill took 3.581 s, then 103.063 ms.
The 4/2 short 128K prefill took 2.490 s, then 89.914 ms.
Do not include model load or first-prefill overhead in decode tokens/s.

The final 145,048-token marker request completed in 192.41 s, with native prefill
190.765 s (760.35 tokens/s), returning `SSD-CONTEXT-7391`. Its ten output tokens are
too few for a stable decode estimate. The longer continuation reused the history,
processed 53 new prompt tokens in the native log, and generated 1,024 tokens in
35.220 s. Ollama reports 145,110 total prompt tokens for that continuation: dividing
that total by its 859 ms partial-prefill duration would be a misleading input rate.

### Failures found and corrected

- **Insufficient runtime VRAM at 2/0.** The 256K model initially had only 96 MiB free
  on GPU1. A 145,048-token request reached about 4K prefill tokens, then CUDA device 1
  failed allocation in `argsort_f32_i32_cuda_cub` / `ggml_cuda_op_top_k`. HTTP 500 after
  8.06 s; runner aborted, Ollama stayed healthy and released it. Host-memory OOM
  counters stayed zero. The identical input passes with 4/2 margins.
- **Native adapter leaked reasoning into final text.** Before
  [#8256](https://github.com/agentydragon/ducktape/pull/8256), native `olm-chat` text
  probes failed all three APIs while tool probes passed. LiteLLM's `ollama/` prefix
  used `/api/generate`; `ollama_chat/` uses `/api/chat` and separates reasoning.
  Public route names and the embedding adapter are unchanged. Post-rollout 128K/256K
  text/tool probes pass on both routes.
- **Background model eviction.** [#8251](https://github.com/agentydragon/ducktape/pull/8251)
  pauses both index workers, retaining their databases. Gatus also loaded HDD
  GPT-OSS every 30 minutes: its probe timed out after 10 s, but Ollama completed that
  request after 3m33s and the queued Qwen request after 4m38s total. This was a
  separate caller, not a routing fallback. [#8254](https://github.com/agentydragon/ducktape/pull/8254)
  disables that generative probe; ordinary Ollama/LiteLLM availability checks remain.

## Reproduction

Deployment sources, shard manifest and operational notes: `cluster/cdk8s/ollama/`.
Verify the setup Job, mounts and blob links before generation.
Keep index workers and the generative health probe paused, and stop exclusive host
inference. Port-forward the serving pod in a separate terminal:

```bash
kubectl -n ollama port-forward service/ollama 19137:11434
```

From the repo's Nix devshell:

```bash
run_dir=/tmp/ollama-ssd-check
mkdir -p "$run_dir"
request=cluster/docs/inference/runs/2026-09-27_ollama_ssd/perf.request.json
curl --fail-with-body --max-time 900 http://127.0.0.1:19137/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-flash-next-iq4xs","keep_alive":"30m","stream":false}' \
  -o "$run_dir/load-128k.json"
curl --fail-with-body --max-time 600 http://127.0.0.1:19137/api/chat \
  -H 'Content-Type: application/json' --data-binary "@$request" \
  -o "$run_dir/perf-128k.json"
jq '{eval_count,eval_duration,prompt_eval_count,prompt_eval_duration,
     decode_tps:(.eval_count*1e9/.eval_duration)}' "$run_dir/perf-128k.json"
```

Repeat the chat request into a separate file for warm measurements. For 256K, replace
the model with `qwen3.8-flash-next-iq4xs-256k` in both requests. Record `/api/ps`, GPU
free memory, cgroup memory events and runner logs for each configuration.

Generate the long request without committing a huge repeated string:

```bash
jq -n '{model:"qwen3.8-flash-next-iq4xs-256k",messages:[{role:"user",
  content:("Remember this exact marker: SSD-CONTEXT-7391. The following is filler.\n"
    + (" pad" * 145000)
    + "\nWhat was the exact marker at the beginning? Reply only with that marker.")}],
  think:false,stream:false,keep_alive:"30m",options:{num_predict:64,seed:42}}' \
  > "$run_dir/long-256k.request.json"
curl --fail-with-body --max-time 900 http://127.0.0.1:19137/api/chat \
  -H 'Content-Type: application/json' --data-binary "@$run_dir/long-256k.request.json" \
  -o "$run_dir/long-256k.response.json"
```

Check `prompt_eval_count > 131072` and the exact marker. For the long continuation,
append the returned assistant message and the user message from `perf.request.json`
to this request, then set `num_predict` to 1,024. Keep the same 256K alias and other
options. Inspect native logs for actually processed versus cached prompt tokens.

For LiteLLM, use the scoped experiment credential without printing it:

```bash
export LITELLM_API_KEY=$(kubectl -n litellm get secret litellm-key-cheap-experiments \
  -o 'jsonpath={.data.api-key}' | base64 -d)
bb run //cluster/debug/litellm_probe:probe_models_bin -- \
  --model ollama/oai-chat/qwen3.8-flash-next-iq4xs-128k \
  --model ollama/olm-chat/qwen3.8-flash-next-iq4xs-128k \
  --shape openai-chat --shape openai-responses --shape anthropic-messages \
  --max-output-tokens 4096 --timeout-seconds 900 \
  --response-dir "$run_dir/litellm-128k" --json
unset LITELLM_API_KEY
```

Run the same matrix with both `-256k` names for that context. The probe is serial and
verifies exact final text and parsed tool name/arguments. These checks do not
constitute a multi-turn coding evaluation.

## Raw evidence

Requests/responses, runner logs, placement, GPU samples and storage inspection are
archived on wyrm2 at
`/var/lib/llm-models-ssd/experiment-artifacts/wyrm2-ollama-ssd-20260927.tar.gz`
(owner-only access). SHA256:
`ef9966cefd51ec32bd6328254c45ced6d70a6c1ffce86ceb34e33bacd58cd665`.
The archive contains the initial failed profile and final `fit4-2/` results;
compact counters and API outcomes are committed beside this note.
