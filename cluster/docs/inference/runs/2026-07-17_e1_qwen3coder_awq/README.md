# E1 — k8s vLLM baseline: Qwen3-Coder-30B-A3B AWQ, TP2, FP8 KV, 262K

- **Status:** in progress (setup applied; awaiting node scheduling)
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

_Pending._

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
