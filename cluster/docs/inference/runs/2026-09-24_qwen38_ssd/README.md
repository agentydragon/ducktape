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
GPU1 startup was 3.84 s and dual startup 19.91 s, both with warm filesystem cache
after checkpoint verification; neither establishes cold SSD loading performance.

An initial Kubernetes hostPath plan was blocked by baseline Pod Security. Automatic
approval rejected relaxing namespace enforcement; that change was not applied.
The experiment uses existing host Docker/CDI instead. No driver, kernel, NixOS
activation or desktop service changes were made. Desktop responsiveness is not
instrumented; sampled GPU allocation and temperature alone do not establish it.

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
2. Repeat dense measurements with substantial input context at 32K.
3. Connect an authenticated private host endpoint through LiteLLM to Agentplane;
   validate real shell tools and coding tasks through both harnesses.
4. Run Flash-Next Q4 with asymmetric GPU headroom and bounded CPU memory; compare
   against the resident control on the same coding tasks and harness.
5. Record exact launch arguments, termination reasons, resource peaks, failures, and
   results here before promoting any configuration.
