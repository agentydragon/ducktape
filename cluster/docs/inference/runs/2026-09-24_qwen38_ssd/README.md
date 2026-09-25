# Qwen3.8 on wyrm2 SSD: work in progress

Started September 24, 2026 (September 25 UTC). No task-quality or throughput result yet.

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
  Downloads started, sequentially, at low CPU/I/O priority with a 60 MiB/s rate cap.
  Credentials come from the existing SOPS-managed `HF_TOKEN`; do not log them.
- Official CUDA server image:
  `ghcr.io/ggml-org/llama.cpp@sha256:014f721265464f38ccb247c1338d07d852c4bae7509a4b4734d07a2bbadc765c`.
  `--version` reports build 11151, commit `bd4f514db`; no model inference yet.
- Use NVIDIA CDI UUIDs. Docker `--gpus device=1` selected a missing AMD CDI spec;
  `--device=nvidia.com/gpu=1` was unresolved because this host's spec has UUID names.
  `--device=nvidia.com/gpu=GPU-6154a49f-2cad-6b72-1e85-09b8006d08b5` launched the
  server's version/help commands successfully. No host runtime changes were made.
- [Ollama pause PR #7907](https://github.com/agentydragon/ducktape/pull/7907) has
  automerge enabled. Deployment zero replicas and bootstrap Job omission retain the
  PVC, credentials, and routing. Verify live GPU release after Flux applies it.

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

1. Confirm GitOps shutdown, GPU health, and checkpoint hashes.
2. Start the dense control on GPU1 at 8K context; verify reasoning and complete
   tool-result round trips with enough output budget.
3. Repeat at 32K, then compare one- and two-GPU placement with measured memory.
4. Run Flash-Next Q4 with asymmetric GPU headroom and bounded CPU memory; compare
   against the resident control on the same coding tasks and harness.
5. Record exact launch arguments, termination reasons, resource peaks, failures, and
   results here before promoting any configuration.
