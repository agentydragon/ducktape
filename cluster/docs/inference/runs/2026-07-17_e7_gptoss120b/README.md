# E7 — gpt-oss-120b, the "edge of runnable" offload case (vLLM TP2, MXFP4)

- **Status:** done — fits only with heavy CPU offload; **~12 tok/s** measured
  (vs the notebook's rough ~1.5 tok/s Ollama estimate). Tool calls work.
- **Date:** 2026-07-17
- **Plan:** <../../PLAN.md> — resident/offload boundary probe

## Goal

Replace the notebook's rough "gpt-oss-120b ≈ 1.5 tok/s (Ollama CPU offload)"
placeholder with a **measured vLLM number**, and answer the user's framing: is
gpt-oss-120b _mostly-resident_ on 2×5090 (→ fast, since only ~5.1B params are
active per token), or does it genuinely need heavy offload (→ slow)?

## The fit: it does NOT fit mostly-resident

gpt-oss-120b is native **MXFP4 ≈ 62 GB**. At TP2 that's ~31.5 GB/GPU, but wyrm2's
display permanently holds ~2.7 GB on GPU0, leaving vLLM only ~28.6 GB there. So
the weight shard alone overflows GPU0:

| `--cpu-offload-gb` | Result                                                  |
| ------------------ | ------------------------------------------------------- |
| 6 (3 GB/GPU)       | **OOM on load** — GPU0 hit 28.6 GB, needed 554 MB more  |
| 12 (per GPU)       | loads (`UVAOffloader`, 12 GB/GPU to pinned host memory) |

So ~12 GB/GPU (~24 GB of the 62 GB, **~38% of weights**) lives in host RAM,
reached over PCIe via UVA. Because gpt-oss is an MoE that routes to a subset of
experts each token, any offloaded expert on the token's path stalls decode — this
is a real offload-tier configuration, not mostly-resident.

Config: vLLM 0.25.1, TP2, `gpu-mem-util 0.83`, `cpu-offload-gb 12`,
`max-model-len 16384`, `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`,
`--tool-call-parser=openai` (reasoning parser auto-set to `openai_gptoss`).
Manifest: <deployment.yaml>.

## Results

### Capacity / resources

| Metric                | Value                                            |
| --------------------- | ------------------------------------------------ |
| Resident weights      | 22.35 GiB/GPU (12 GiB/GPU offloaded to host RAM) |
| Peak VRAM             | GPU0 29.9 GB / GPU1 27.9 GB                      |
| GPU KV cache          | 118,752 tokens (2.47 GiB — most VRAM is weights) |
| Max concurrency @ 16K | 7.25×                                            |
| Model load time       | 653 s (UVA offload staging + torch.compile)      |

### Latency / decode (thinking on; offload)

| Input ctx | TTFT  | TTFC (first content) | Decode tok/s |
| --------- | ----- | -------------------- | ------------ |
| 2K        | 1.9 s | 11.4 s               | **12.1**     |
| 8K        | 6.3 s | 15.0 s               | **12.4**     |

**~12 tok/s** — the headline result. That is ~8× the notebook's prior
placeholder (~1.5 tok/s via Ollama), because vLLM keeps **62% of the weights
resident** and only spills 38% to CPU, whereas the Ollama config offloaded far
more. It is still an **offload-tier** speed: an order of magnitude below the
resident MoEs (gpt-oss-20b ~1000, Qwen A3B ~200), but usable for non-interactive
work. TTFC is 11–15 s because gpt-oss reasons first (in the `reasoning` channel)
before emitting content.

### Tool calls — work (single + multi-turn)

`single` ✓ and `multi_turn` ✓ (coherent weather reply); `parallel` ✗ (emitted 1
of the parallel calls). Identical profile to gpt-oss-20b (E2), and far better
than Qwen3.6 (E6), which emitted no parseable tool calls at all. gpt-oss is the
better-behaved tool-caller of the models benched here.

## Verdict

gpt-oss-120b is the **intelligence-per-latency ceiling of the offload lane** on
2×5090: a 120B model (62.4 SWE-bench Verified, 80.9 GPQA Diamond) running at ~12
tok/s with working tool calls. It doesn't fit resident (62 GB MXFP4 vs ~58 GB display-reduced budget →
38% CPU offload mandatory), so it's ~16× slower than the resident A3B MoEs — but
it's a genuinely stronger model than anything that fits resident, and 12 tok/s is
usable for batch/agent work where latency isn't critical. The `{intelligence,
latency}` trade is explicit: pay ~16× latency for the bigger model.

## Notes

- The download+load hit an **infrastructure incident** (the `lvm-proxmox-ssd`
  thin pool filled to 100% → PVC `emergency_ro`), fixed by recreating `hf-cache`
  at 400Gi so openebs provisioned a 400 GB pool. Full write-up:
  <../../lessons_learned/2026_07_17_lvm_thinpool_exhaustion_emergency_ro.md>.
- ollama's Flux Kustomization was suspended (PR #3389) so it stops re-grabbing
  the GPUs during the bench pods' restart gaps.
