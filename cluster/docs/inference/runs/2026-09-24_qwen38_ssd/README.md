# Qwen3.8 on wyrm2 SSD: work in progress

Started September 24, 2026 (September 25 UTC). Initial protocol and throughput results below; coding-task quality remains unmeasured.

[Download and probe reproduction commands](REPRODUCE.md) complement the pinned
launch scripts and captured responses below.

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
  Dense Q8 and all four Flash-Next Q4 shards are downloaded and SHA256 verified.
  Downloads ran sequentially,
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
  merged as `afe5c6437f7ade63dccfafd3fdd91e8fea4dcf27`. At the September 24 experiment checkpoint, Deployment was 0/0,
  Flux was Ready/Healthy, the model PVC remained Bound, and GPU1 usage fell to 5 MiB.
  Ollama was subsequently re-enabled by #8000; this is historical run state.
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

## Tensor parallelism: container NCCL repair and measurements

The base CUDA image packages NCCL 2.25.1. Tensor splitting failed during warmup
with CUDA `invalid argument`, an NCCL shared-memory limit warning (82,240 versus
79,856 bytes), and process exit 139. Docker reported no OOM; GPU allocations were
released. The second attempt enabled `NCCL_DEBUG=INFO` to capture the cause:
[initial log](dense_tensor_failure.txt), [diagnostic log](dense_tensor_nccl_failure.txt).
This matches a previously reported [NCCL/5090 issue](https://discuss.pytorch.org/t/torch-distributed-distbackenderror-nccl-error-in-pytorch-torch-csrc-distributed-c10d-processgroupnccl-cpp-3368-unhandled-cuda-error-run-with-nccl-debug-info-for-details-nccl-version-2-25-1/221360/2).

Replacing only the container's `libnccl2` with NVIDIA's SHA256-pinned
2.27.7-1+cuda12.9 package resolved this startup failure. No host packages, driver,
kernel, or services changed. [Dockerfile](Dockerfile.nccl227):

```bash
docker build -t wyrm2-llama-nccl:2.27.7 -f Dockerfile.nccl227 .
bash dense_tensor.sh
```

The measured derived image ID was
`sha256:85a6cda88b8fe62e22255615a7aab7984e836249a3cba1000911f88bda185cd9`.
Tensor mode lacks automatic fitting, so its [launcher](dense_tensor.sh) checks free
GPU memory and permits at most the measured 32K context for this dense checkpoint.
It disables the unsupported fitter explicitly; the measured command supplied
`--fit-target`, which emitted a warning and had no effect.

| Actual input tokens | Generated tokens | Prefill ms | Prefill tokens/s | Decode ms | Decode tokens/s |
| ------------------- | ---------------: | ---------: | ---------------: | --------: | --------------: |
| 102                 |             1807 |    164.953 |           618.36 | 25136.393 |           71.85 |
| 24132               |             1670 |   7884.432 |          3060.72 | 24463.114 |           68.23 |

Both requests ended with `stop`. GPU allocations during the long sample were
17,392 / 14,670 MiB (desktop included on GPU0), leaving about 14 GiB free on GPU0.
Both GPUs reached about 94% utilization; sampled temperatures were 52/61 C.
The same prompts and output caps were used, but generated token counts changed
with this execution mode despite temperature zero. Thus these token rates suggest
about 1.4 times the one-GPU decode throughput, not a matched-output task speedup.
Tensor prefill was slower than layer splitting. Coding quality and desktop
responsiveness under sustained tensor inference remain unmeasured.
Raw [short](dense_tensor_nccl227_32k_short.json) and
[long](dense_tensor_nccl227_32k_long.json) responses preserve those differences.

## Flash-Next Q4 initial feasibility

All four checkpoint shards passed their pinned size/SHA256 checks. The successful
8K launch used both GPUs, automatic placement with free-memory targets of 8 GiB
on GPU0 and 2 GiB on GPU1, a 38 GiB container memory limit, no swap, eight CPU
threads, mmap, lazy embedding-table reads, and no CPU weight repacking. The launcher
requires at least 54 GiB host available RAM immediately before launch, leaving a
16 GiB margin against its hard cap: [flash_dual.sh](flash_dual.sh).

The first attempt inherited the dense control's explicit `--tensor-split 1,1`.
Because this model required refitting, llama.cpp refused to adjust that explicit
split. That attempt was stopped immediately after observing the warning, and the
explicit split was removed before retrying. No OOM was observed.
[Failed-attempt log](flash_explicit_split_fit_failure.txt).

The successful server became ready at 88.13 s; cache state was uncontrolled.
[Server log](flash_8k_server.txt). Initial Docker working-set display was 23.05 GiB,
but that excludes inactive file cache: later cgroup memory peak reached the full
38 GiB limit, with reclaim events and zero OOM/kill events. Host available RAM was
still about 54 GiB. [Cgroup snapshot](flash_8k_resources.txt).
During generation, GPU allocations were 23,939 / 29,700 MiB including the desktop,
leaving roughly 8 GiB on GPU0. No user-authored data was moved or deleted.

The same short coding request produced 1,776 tokens with `stop`: 1.378 s prefill,
54.234 s decode, **32.73 tokens/s**. This is one generation, not a correctness score.
[Request](flash_coding_request.json), [response](flash_dual_8k_coding.json).
An earlier arithmetic probe returned 323 for 17×19; its four generated tokens are
not a useful throughput sample.

### Tool grounding failure retained

The first reasoning-enabled `read_file` request made the expected call. The supplied
result contained only `verification_code=SSD-5090-7C2E`. The final answer repeated
that code but invented an embedded `<agent_instructions>` block and a prompt-injection
warning. Thus the protocol worked but **this grounding check failed**.
Exact [first request](flash_tool_request.json), [first response](flash_tool_response.json),
[follow-up request](flash_tool_followup_request.json) and
[follow-up response](flash_tool_followup_response.json) are retained.

A repeat with `cache_prompt=false` and seed 42 returned the code without that claim.
A second repeat with the same seed and caching enabled also returned the same clean
answer (369 cached tokens versus zero). This does not isolate the first failure to
caching: the initial run had no fixed seed, and both controlled variants succeeded.
See the `flash_tool_followup_uncached_*` and `flash_tool_followup_cached42_*` artifacts.
Do not promote this model from parameter count, one clean retry or token rate alone.

## Flash-Next 32K follow-up

The same launcher with `CONTEXT_SIZE=32768` completed the 24,132-token input
used for the dense comparison. The response stopped normally after 1,697 output
tokens: prefill **126.703 s / 190.46 tokens/s**, decode **55.217 s / 30.72 tokens/s**,
combined server time **181.920 s**. This is a single sample with zero cached input
tokens, medium reasoning effort, and a 2,048-token output cap. The generated code
was not executed or scored. [Raw response](flash_dual_32k_long.json).

The model alias was changed to `qwen3.8-flash-next-q4`; the source payload and coding
instruction match the dense long-input probe. At 32K configured context this
measures a filled 24K input, not 128K capacity or long-context retrieval quality.
The prefill cost makes prompt reuse a priority for subsequent agent experiments.

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

The September 26 restart plan and the operator's subsequent Harbor/Ollama
observations are in [the program plan](../../PLAN.md). In particular, first compare
fresh prefill with growing cached conversations, then map 128K/256K and concurrent
slots. Keep coding task definitions and scoring outside Agentplane; shared gateway
integration is optional and follows a useful configuration.
