# E1 — k8s vLLM baseline: Qwen3-Coder-30B-A3B AWQ, TP2, FP8 KV, 262K

- **Status:** done — 262K allocated & effective, tool calls clean; reference row landed
- **Date:** 2026-07-17
- **Plan:** <../../PLAN.md> § "First five experiments" → E1

## Goal

Port the known-good wyrm2 host vLLM config (<../../vllm_history.md>) into
Kubernetes and confirm the whole k8s + CDI + vLLM path works, then produce the
reference row in <../../results.md>.

## Configuration

- **Model:** `cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit` (AWQ 4-bit,
  compressed-tensors; auto-detected — no `--quantization` flag).
- **Runtime:** `vllm/vllm-openai:latest` (actual version/digest recorded once
  the pod is up).
- **Key args:** `--tensor-parallel-size 2 --max-model-len 262144
--kv-cache-dtype fp8 --gpu-memory-utilization 0.90 --max-num-seqs 32
--enable-auto-tool-choice --tool-call-parser qwen3_coder`.
- **Namespace:** `llm-bench` (ad-hoc; not Flux-managed).
- **Storage:** `hf-cache` PVC, 120Gi, `lvm-proxmox-ssd` (SSD LVM on wyrm2).
- **/dev/shm:** 16Gi `emptyDir{medium:Memory}` — required for TP2 NCCL.

Apply with:

```bash
kubectl apply -f namespace.yaml -f pvc.yaml -f service.yaml -f deployment.yaml
```

## What to measure

- Allocated context at 128K and 256K (largest that loads + completes a request).
- TTFT and decode tokens/s at 8K / 32K / 128K input (temp 0, 256-token output).
- Tool-call smoke: single, parallel, multi-turn with fixed schemas.
- Peak per-GPU VRAM (`nvidia-smi` on wyrm2).

Quality: external Qwen3-Coder numbers (`ext`) — not re-run here.

## Results

Runtime: **vLLM 0.25.1** (`vllm/vllm-openai:latest`), AWQ 4-bit auto-detected as
compressed-tensors → Marlin WNA16 MoE kernels, FlashInfer attention. TP2 over the
two 5090s with **no GPU P2P** (PYNCCL all-reduce fallback, as expected on the
Proxmox-passthrough VM). Cold start (this run): model download 16.85 GiB, weights
load 8.56 GiB/GPU in 32 s, engine init/compile 124 s.

### Capacity / resources

| Metric                           | Value                                              |
| -------------------------------- | -------------------------------------------------- |
| Advertised context               | 262,144                                            |
| **Allocated context**            | **262,144** (255,669-token request completed)      |
| **Effective context** (`local~`) | **≥262,144** — needle hits at depths 0.1/0.5/0.9   |
| GPU KV cache                     | 815,504 tokens                                     |
| Max concurrency @ 262K           | 3.11×                                              |
| Peak VRAM (steady state)         | GPU0 30.7 GB / GPU1 29.9 GB of 32.6 GB (0.90 util) |

Effective context is a quick needle probe (a few needles at 3 depths, exact
match) — `local~`, catches gross breakage, not subtle degradation. For fine
long-context quality, defer to external RULER-style Qwen3-Coder results.

### Latency (single request, temp 0, 256-token output, 3 reps)

TTFT splits sharply by prefix-cache state: **p50 ≈ warm-prefix** (cache hit on
reps 2–3), **p95 ≈ cold-prefill** (the first, uncached rep). Decode rate is
unaffected by caching.

| Input ctx | TTFT warm (p50) | TTFT cold (p95) | Decode tok/s |
| --------- | --------------- | --------------- | ------------ |
| 8K        | 0.18 s          | 0.77 s          | 271          |
| 32K       | 0.40 s          | 3.0 s           | 235          |
| 128K      | 0.45 s          | 27.6 s          | 199          |

Cold 128K prefill ≈ 27 s is the honest first-request number; a warm agent loop
re-hitting the same prefix sees ~0.5 s.

### Tool calls (`qwen3_coder` parser)

Single ✅ · parallel ✅ (Paris + Tokyo in one turn) · multi-turn ✅ (consumed the
tool result: "18°C and sunny"). No parse failures.

### Quality

External Qwen3-Coder numbers (`ext`) — not re-run here per the PLAN's quality
policy.

### Verdict

The known-good wyrm2 host config reproduces cleanly in Kubernetes: the whole
k8s/CDI/vLLM path works end-to-end, 262K is both allocated and needle-effective,
tool calling is clean, and decode holds ~200–270 tok/s. This is the reference row
in <../../results.md>. Raw measurements: <summary.json>.

## Notes / anomalies

- **Node recovery first:** wyrm2 was `NotReady` at the start of this run
  (haproxy 3.3 frontend/backend name collision after a nixos switch — fixed in
  a separate PR; see
  <../../../lessons_learned/2026_07_17_haproxy_33_frontend_backend_naming.md>).
- **Disk pressure:** after recovery, wyrm2's root fs was ~90% full, tainting the
  node `disk-pressure:NoSchedule` and blocking GPU pods. Cleared by nix GC before
  scheduling.
- **One model at a time:** vLLM is one-process-per-model and the two 5090s hold
  64 GB total, so a running vLLM experiment contends with the live Ollama
  deployment for both GPUs. The Ollama Deployment is scaled down for the duration
  of a GPU experiment and restored afterward.
- **PodSecurity:** the `vllm/vllm-openai` image runs as root; `llm-bench` emits
  `restricted:latest` PodSecurity warnings (warn-only, not enforced). Acceptable
  for an ad-hoc bench namespace.
