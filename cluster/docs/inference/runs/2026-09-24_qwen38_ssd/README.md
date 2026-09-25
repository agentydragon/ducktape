# Qwen3.8 on wyrm2 SSD: work in progress

Started September 24, 2026 (September 25 UTC). Initial protocol and throughput results below; coding-task quality remains unmeasured.

## Scope

Compare a resident Qwen3.8-27B Q8 control with Qwen3.8-Flash-Next Q4 using both
5090s and CPU offload. GPU1-only is the initial smoke probe, not the final hardware
target. Compare one- and two-GPU placement explicitly; leave headroom on GPU0 for
the desktop. A larger/higher-precision working set is a useful two-GPU gain even
if decode throughput does not scale proportionally.

The operator authorized model downloads, host inference experiments, GitOps shutdown
of cluster Ollama, verified SSD-to-HDD model archival, and focused PR automerge.
No reboot, GPU reset, full NixOS activation, or disruption of desktop use or authored
data. Report observed interference. Do not change drivers or kernel for these trials.

## Inputs and current progress

- SSD mount `/var/lib/llm-models-ssd`: 1 TiB, initially 515 GiB available.
- [Checkpoint manifest](checkpoints.json): pinned Hugging Face revisions, file sizes,
  and SHA256 hashes. Qwen3.8-27B Q8 is 29.05 GB; Flash-Next Q4 is 111.33 GB.
  Dense Q8 downloaded and SHA256 verified; Flash-Next download continues sequentially,
  at low CPU/I/O priority with a 60 MiB/s rate cap.
  Credentials come from the existing SOPS-managed `HF_TOKEN`; do not log them.
- Official CUDA server image:
  `ghcr.io/ggml-org/llama.cpp@sha256:014f721265464f38ccb247c1338d07d852c4bae7509a4b4734d07a2bbadc765c`.
  `--version` reports build 11151, commit `bd4f514db`.
- Use NVIDIA CDI UUIDs. Docker `--gpus device=1` selected a missing AMD CDI spec;
  `--device=nvidia.com/gpu=1` was unresolved because this host's spec has UUID names.
  `--device=nvidia.com/gpu=GPU-6154a49f-2cad-6b72-1e85-09b8006d08b5` launched the
  server's version/help commands successfully. No host runtime changes were made.
- [Ollama pause PR #7907](https://github.com/agentydragon/ducktape/pull/7907) has
  merged as `afe5c6437f7ade63dccfafd3fdd91e8fea4dcf27`. Live Deployment is 0/0,
  Flux Ready/Healthy, model PVC remains Bound, and GPU1 usage fell to 5 MiB.
  Deployment zero replicas and bootstrap Job omission retain credentials and routing.

## Initial resident dense results

Both configurations use the pinned Q8 checkpoint, 8K configured context, Q8 KV cache,
flash attention, one slot, eight CPU threads, and the same CUDA image. Host Docker
publishes only loopback port 18080. Container RAM is capped at 16 GiB with no swap.
Launch commands: [GPU1](dense_gpu1.sh), [both GPUs](dense_dual.sh).
The dual configuration reserves 8 GiB on desktop GPU0 and 2 GiB on GPU1.

| Placement                   | Prompt tokens | Generated tokens | Prefill ms | Decode ms | Decode tokens/s | GPU0 / GPU1 allocated MiB |
| --------------------------- | ------------: | ---------------: | ---------: | --------: | --------------: | ------------------------- |
| GPU1 only                   |           102 |             2036 |    123.456 | 39829.257 |           51.09 | ~2728 / 27132             |
| Two GPUs, equal layer split |           102 |             2036 |    139.131 | 40698.275 |           50.00 | 16466 / 14248             |

One request per placement, temperature zero, medium reasoning, 2048-token output
budget. Both ended with `stop` and complete code. GPU0 totals include the desktop.
This is server-reported token timing, not end-to-end agent task latency. The request
asks for stable topological sorting; generated code has not yet been executed or
scored. Raw [request](coding_request.json), [GPU1 response](dense_gpu1_coding.json),
and [dual response](dense_dual_coding.json) retain exact timing and usage fields.
No single-stream speedup is demonstrated by this layer-split sample. Two GPUs halve
per-card model allocation, leaving space for larger working sets. Other split modes,
batching, long prompts and task quality remain unmeasured.

The GPU1 protocol gate also completed a two-turn `read_file` tool call with reasoning
and returned a synthetic verification code from the supplied tool result. Its two
responses decoded at 50.71 and 50.82 tokens/s. This is a synthetic tool-protocol gate,
not a real filesystem operation or coding benchmark; see
[dense_tool_roundtrip.json](dense_tool_roundtrip.json).
GPU1 startup was 3.84 s and dual startup 19.91 s for the initial 8K trials.
Filesystem cache was not controlled and checkpoint verification read the dense file
beforehand; neither establishes cold SSD loading performance.

An initial Kubernetes hostPath plan was blocked by baseline Pod Security. Automatic
approval rejected relaxing namespace enforcement; that change was not applied.
The experiment uses existing host Docker/CDI instead. No driver, kernel, NixOS
activation or desktop service changes were made. Desktop responsiveness is not
instrumented; sampled GPU allocation and temperature alone do not establish it.

## Filled-context measurements

Set `CONTEXT_SIZE=32768` with the same launchers. Prepend exactly 24,000 tokenizer
IDs of repository source to the same coding question; chat formatting and task bring
the actual input to 24,132 tokens. This measures processing unrelated source context,
not whether the model can retrieve or reason over it. No prompt-cache hits were
reported. One request per placement, both `stop`, both 1,758 generated tokens.

| Placement                   | Prefill ms | Prefill tokens/s | Decode ms | Decode tokens/s | Sampled GPU0 / GPU1 allocated MiB |
| --------------------------- | ---------: | ---------------: | --------: | --------------: | --------------------------------- |
| GPU1                        |   7173.672 |          3363.97 | 36799.951 |           47.74 | 2727 / 28058                      |
| Two GPUs, equal layer split |   4813.582 |          5013.31 | 38049.605 |           46.18 | 17063 / 14840                     |

Dual-GPU prefill was about 1.49 times as fast in this sample, while decode was about
3% slower. Total server prompt-plus-decode time was 42.86 s dual versus 43.97 s on
GPU1. This does not establish a task-quality advantage or a statistically stable
performance difference. The concurrent rate-capped model download was active for
both placements. No cache eviction or desktop shutdown was performed.

Exact responses: [GPU1](dense_gpu1_32k_long.json),
[dual](dense_dual_32k_long.json). [Source manifest](long_context_sources.json) records
the ordered files, content hashes and full request hash. The local request is retained
at `/tmp/wyrm2-llm-experiments-2026-09-24/long_context_request.json`; its repository
payload is not duplicated here. An initial undersized context probe had 2,358 prompt
tokens and is excluded from this matched comparison.

## Agentplane evidence to reuse

[PR #7898](https://github.com/agentydragon/ducktape/pull/7898) adds a deployed
tool-call smoke matrix through Claude and Codex. GPT-OSS 20B and Gemma 31B had
successful cells; first attempts also showed cold-load and harness failures.
Those runs used the **HDD-backed Ollama PVC**, confirmed as `lvm-proxmox-hdd`.
The 120B cold-load timeout does not establish an SSD-backed feasibility limit.
The five-minute smoke deadline and tool-only scoring are unsuitable as coding-quality
or long-context conclusions. New runs separate storage/loading, protocol/tool
correctness, prefill, reasoning/decode, and total task completion time.

## Next evidence

1. Finish Flash-Next checkpoint download and SHA256 verification.
2. Evaluate alternate two-GPU splitting where supported; 32K filled-input baseline is recorded above.
3. Connect an authenticated private host endpoint through LiteLLM to Agentplane;
   validate real shell tools and coding tasks through both harnesses.
4. Run Flash-Next Q4 with asymmetric GPU headroom and bounded CPU memory; compare
   against the resident control on the same coding tasks and harness.
5. Record exact launch arguments, termination reasons, resource peaks, failures, and
   results here before promoting any configuration.
