# Qwen3.8-Flash-Next capacity arithmetic, September 26

Read-only source/header analysis, not a capacity or quality benchmark. All future
inference remains serial unless the operator changes that constraint. Parallelism
below means independent resident conversation windows sharing one model instance;
it does not mean duplicate model processes or a measured throughput gain.

## Inputs and derivation

Runtime: llama.cpp `bd4f514db14d87fded667787a7a963bfbaa98e89`, the digest in the
[September 24 launcher](../2026-09-24_qwen38_ssd/flash_dual.sh).
Checkpoint: Unsloth revision `38bb39ee97821de2c9009abb7e93950eec396e66`.
[gguf_metadata.json](gguf_metadata.json) was read from the local Q4 GGUF headers.
K means 1,024 tokens; all memory below is GiB, not decimal GB. Context includes
prompt and generated tokens, so output reserve reduces usable input length.

There are 12 full/sparse attention layers and 36 recurrent layers. Each attention
layer caches two 256-wide key/value heads plus a 128-wide indexer key. The pinned
[indexer implementation](https://github.com/ggml-org/llama.cpp/blob/bd4f514db14d87fded667787a7a963bfbaa98e89/src/llama-memory-hybrid-idx.cpp)
retains indexer keys per token and pools them at read time: its compress ratio of
four does not divide the stored indexer cache size by four.

For N total independently retained tokens:

```text
cache_bytes = N * 12 * ((512 + 128) * bytes_per_K + 512 * bytes_per_V)
```

Use actual block storage: F16/BF16 2 bytes/value; Q8_0 34/32; Q5_0 22/32;
Q5_1 24/32; Q4_0 18/32; Q4_1 20/32. Thus Q8/Q8 costs 14,688 bytes/token,
Q5_0/Q5_0 9,504, and Q4_0/Q4_0 7,776. Scales are included.

The pinned [model memory constructor](https://github.com/ggml-org/llama.cpp/blob/bd4f514db14d87fded667787a7a963bfbaa98e89/src/llama-model.cpp)
uses F32 for recurrent state regardless of K/V quantization. With rollback disabled,
each sequence additionally needs:

```text
4 * (36 * (128*6144 + 3*(6144 + 2*16*128)) + 3*3*4*2560)
= 118,038,528 bytes = 0.10993 GiB
```

This includes delta/conv state and the one PLE conv history. Rollback snapshots
multiply recurrent rows by `1 + n_rs_seq`; server checkpoints, prompt caching,
metadata, graph buffers and allocator overhead are additional. No MTP is included.

## Persistent cache size

The table excludes the additional 0.110 GiB per sequence and temporary workspaces.
[cache_capacity.json](cache_capacity.json) retains unrounded arithmetic and slot
counts. No claim that the runtime can allocate or use every listed window reliably.

| K / V       |  128K |  256K |   512K |     1M |
| ----------- | ----: | ----: | -----: | -----: |
| F16 / F16   | 3.375 | 6.750 | 13.500 | 27.000 |
| Q8_0 / Q8_0 | 1.793 | 3.586 |  7.172 | 14.344 |
| Q8_0 / Q5_0 | 1.512 | 3.023 |  6.047 | 12.094 |
| Q8_0 / Q4_0 | 1.418 | 2.836 |  5.672 | 11.344 |
| Q5_1 / Q5_1 | 1.266 | 2.531 |  5.062 | 10.125 |
| Q5_0 / Q5_0 | 1.160 | 2.320 |  4.641 |  9.281 |
| Q4_1 / Q4_1 | 1.055 | 2.109 |  4.219 |  8.438 |
| Q4_0 / Q4_0 | 0.949 | 1.898 |  3.797 |  7.594 |

Native context is 256K. The 512K/1M columns are storage arithmetic; they require
correctly supported context extension and separate quality checks. Spare memory does
not extend the model's trained context automatically. Mixed Q8/Q4 saves 20.9% here,
not the generic 25% estimate, because the extra indexer storage follows K precision.

If an **8 GiB budget remains after weights and runtime workspaces**, including
recurrent state but excluding further per-slot overhead, the integer ceilings are:

| K / V       | Independent 128K windows | Independent 256K windows |
| ----------- | -----------------------: | -----------------------: |
| F16 / F16   |                        2 |                        1 |
| Q8_0 / Q8_0 |                        4 |                        2 |
| Q8_0 / Q4_0 |                        5 |                        2 |
| Q5_0 / Q5_0 |                        6 |                        3 |
| Q4_0 / Q4_0 |                        7 |                        3 |

Two 128K sessions cost 3.806 GiB with Q8 or 2.118 GiB with Q4; two 256K sessions
cost 7.392 or 4.017 GiB respectively. Four 128K sessions have the same token count
but more recurrent state: 7.612 GiB Q8 or 4.237 GiB Q4. Useful throughput and tail
latency at these counts remain unknown. Independent agent prompts get no assumed
shared-prefix saving. KV storage fitting does not promise a parallel speedup.

## Weights and offload

[weight_sizes.json](weight_sizes.json) records pinned artifact totals from the HF
API and PLE tensor sizes/types from small GGUF header range reads. Non-PLE includes
small file/header overhead; it is a storage approximation, not peak resident memory.
Remote header reads are not a full checkpoint checksum. The local Q4 files were
previously hash-verified; the download queue verifies each newly completed shard.

| Existing Unsloth quant | Total files | PLE lookup table | Other weights and headers |
| ---------------------- | ----------: | ---------------: | ------------------------: |
| UD-IQ1_S               |       67.56 |            26.82 |                     40.74 |
| UD-IQ1_M               |       69.42 |            26.82 |                     42.60 |
| UD-Q2_K_XL             |       73.45 |            26.82 |                     46.63 |
| UD-IQ3_XXS             |       76.33 |            26.82 |                     49.51 |
| UD-Q3_K_XL             |       83.81 |            26.82 |                     56.98 |
| UD-IQ4_XS              |       87.25 |            26.82 |                     60.43 |
| UD-Q4_K_XL, current    |      103.69 |            26.82 |                     76.87 |
| UD-Q5_K_XL             |      147.42 |            50.66 |                     96.75 |
| UD-Q6_K_XL             |      157.55 |            50.66 |                    106.88 |
| Q8_0                   |      175.30 |            50.66 |                    124.63 |

The SSD-backed lookup table is not required to be completely resident. All experts
are still present; 10-of-512 routing does not mean only 10 experts need storage.
Other weights must either fit across GPU/host RAM or page from SSD, with unmeasured
latency consequences. An SSD file saving is not automatically equal VRAM freed.

The live read at approximately 02:46 Pacific had 60.28 GiB combined free VRAM.
Retaining the launcher's additional 8 GiB GPU0 and 2 GiB GPU1 headroom leaves roughly
50 GiB for model allocation. The cards remain separate devices; layer placement and
uneven allocation can constrain them before their combined budget is exhausted.
As an illustrative partition, reserving 3 GiB for GPU workspace and 8 GiB for state
leaves 39 GiB for GPU weights. Then the minimum remaining non-PLE weight storage is:

| Weight quant | Non-PLE weights outside that 39 GiB GPU budget |
| ------------ | ---------------------------------------------: |
| Q5_K_XL      |                                      57.75 GiB |
| Q4_K_XL      |                                      37.87 GiB |
| IQ4_XS       |                                      21.43 GiB |
| Q3_K_XL      |                                      17.98 GiB |
| IQ3_XXS      |                                      10.51 GiB |
| Q2_K_XL      |                                       7.63 GiB |
| IQ1_S        |                                       1.74 GiB |

These are aggregate storage lower bounds, not a verified device placement. Add PLE
page cache and host overhead. Q4 plus an 8 GiB state allocation is therefore tight
under our old 38 GiB host cgroup cap; a 4 GiB state allocation leaves about 43 GiB
for GPU weights and 33.87 GiB of non-PLE weights outside GPU, giving more host margin.
IQ4_XS releases 16.44 GiB versus Q4 without enlarging PLE, a larger capacity lever
than the 1.69 GiB saved by changing one 256K window from Q8 to Q4 KV.

Current host MemAvailable was only 33 GiB; preserving 16 GiB would leave about 17
GiB for additional host allocation. The historical 38 GiB envelope is not presently
a safe launch assumption without checking which existing allocations can be released.
No process or workload was stopped for these calculations.

## Activations, kernel support and quality

KV is persistent history; intermediate activations are temporary workspace. The
pinned llama.cpp path does not expose a general activation-bits knob comparable to
`--cache-type-k/v`. Lower microbatch size can reduce workspace at a prefill-speed
cost without deliberately reducing numerical precision. Recurrent state remains
F32. Do not apply a global 2x/4x memory saving to activations or all allocations.

The pinned [CUDA attention source](https://github.com/ggml-org/llama.cpp/blob/bd4f514db14d87fded667787a7a963bfbaa98e89/ggml/src/ggml-cuda/fattn.cu)
accepts Q4_0, Q4_1, Q5_0, Q5_1 and Q8_0 and has an F16-temporary path when a direct
quantized kernel is unavailable. Thus the earlier generic CPU-fallback warning is
not proof this revision has that issue. The actual binary/model path still needs
verification. IQ4_NL is CLI-listed but not accepted by that CUDA FA type switch;
it is not included as a supported GPU-cache candidate here. F16 temporaries can
require about 0.25 GiB per 128K tokens for one layer's K+V alone; buffer lifetimes
and graph allocation determine the peak, which this analysis has not measured.

Other formats do exist: official [FP8](https://huggingface.co/Qwen/Qwen3.8-Flash-Next-FP8)
is 172.78 GiB, NVIDIA's [NVFP4 W4A4 experts](https://huggingface.co/nvidia/Qwen3.8-Flash-Next-NVFP4)
123.57 GiB, and a community [NVFP4 PLE variant](https://github.com/starkweatherdigital/qwen3.8-flash-next-nvfp4-recipe)
101.68 GiB. Exact file totals/revisions are in [alternate_formats.json](alternate_formats.json).
They require a different runtime/placement investigation, not a GGUF activation flag;
none fits entirely in these GPUs. NVIDIA reports near-FP8 quality on its published
evals (Terminal-Bench 2.1: 82.9 versus 83.3), but tests on B200/B300 and retains other
components at higher precision. This does not validate our GGUF, a community PLE
requantization, or Terminal-Bench 4.0 performance.

Quality expectations are hypotheses, not measured task-loss percentages:

- Q8 KV is the conservative control. Q5 KV is a plausible lower-risk capacity step;
  Q8 K/Q4 V preserves keys and the indexer at Q8 but saves less memory.
- Q4 KV approaches 1.89x raw token capacity versus Q8, with greater uncertain risk
  to long-context retrieval and trajectories. No model-specific agent-quality result
  was found that settles the trade-off. Do not call it harmless or crippling yet.
- IQ4_XS is the first smaller-weight comparison; Q3 and IQ3 are more aggressive.
  The quantizer's [distribution comparison](https://unsloth.ai/docs/models/qwen3.8-next#quantization-analysis)
  reports mean KLD 0.0469 for Q4, 0.0836 IQ4_XS, 0.1065 Q3, 0.1651 IQ3, 0.2246 Q2,
  and 0.3961 IQ1_S. Q5 is 0.0304. These rank distribution distortion, not coding pass
  rates; apparent accuracy-recovery percentages there are not task retention.
- IQ1/2 could make more of the compute weights GPU-resident, but their greater
  distortion makes them poor initial choices for a capability-first program.

First test fixed Q4 weights with Q8, Q5 and Q4 KV serially at matched long context;
then IQ4_XS weights with the selected cache configuration. Q5 weights remain a
quality control, not the expected winner for capacity. No concurrent evals launched.

## Downloads started

The authorized queue is Q5_K_XL then IQ4_XS, totaling 234.66 GiB. Starting SSD free
space was about 384 GiB, leaving about 149 GiB after both. No expansion or deletion
is needed. This bounded queue uses a 128 GiB free-space floor rather than the older
200 GiB download-script reserve; it writes directly to resumable partial files,
verifies SHA256, then renames without a second full-size copy. Disk usage by another
process can still halt the queue at that floor.

```bash
bash cluster/docs/inference/runs/2026-09-26_qwen38_capacity/download.sh
```

[download.sh](download.sh) uses public pinned URLs, 40 MiB/s, serial downloads and
checksums from [downloads.tsv](downloads.tsv). It requires the mounted model SSD;
no credentials are logged. The running command additionally uses `nice -n 10` and
`ionice -c 3`. Its log is `/tmp/qwen38-quant-download.log`. Q5 was observed progressing;
completion of either whole checkpoint is not claimed in this note. Pause downloads
and checksum scans before measuring inference latency.
