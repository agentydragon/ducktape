# 2026-07-14 GLM-5.2 via Colibri on wyrm2

Host-side experiment running the 744B-parameter GLM-5.2 MoE through Colibri's
disk-streamed expert runtime on wyrm2's two RTX 5090s. The model ran coherently,
but the best full-quality result was **0.28 tok/s**, below the 0.5 tok/s gate for
adding it to cluster LiteLLM.

> **Status (2026-07-14):** experiment complete; checkpoint retained, LiteLLM
> integration deferred.

## Reproducibility bundle

- **Upstream runtime:** [JustVugg/colibri](https://github.com/JustVugg/colibri)
  at `6d3ed7e62b1b4c05d8e656a5263e91b983aa26ba`
- **Checkpoint:** `mateogrgic/GLM-5.2-colibri-int4-with-int8-mtp` at
  `3cc8db99b1b13fc79325d987ba3c1c430766b3b8`
- **Nix environment:** <flake.nix> and <flake.lock>
- **Checkout/build driver:** <setup.sh>
- **Checkpoint download and integrity gate:** <download.sh> and
  <verify_checkpoint.sh>
- **Exact benchmark sequence:** <run_benchmarks.sh>
- **Disposable upstream fixture-parser compatibility patch:**
  <benchmark_cuda_fixture.patch>

The bundle snapshots the experimental environment; it does not vendor Colibri or
the 383.8 GB checkpoint. Run the scripts from this directory on wyrm2. By default,
the checkout is placed at `/var/lib/colibri/src/colibri`, the model at
`/var/lib/colibri/glm-5.2-colibri-int4-with-int8-mtp`, and new logs under
`./results/`.

```bash
./setup.sh
./download.sh
./verify_checkpoint.sh
./run_benchmarks.sh
```

`run_benchmarks.sh` temporarily moves any existing `.coli_usage` history out of
the model directory, starts from a clean routing history, captures the new history,
and restores the original on exit. This makes the five-run refinement sequence
repeatable without clobbering later interactive usage.

## Hardware and placement

| Resource                    | Experiment value                                                 |
| --------------------------- | ---------------------------------------------------------------- |
| GPU                         | 2 x RTX 5090, 32,607 MiB each                                    |
| CPU                         | Ryzen 9 9950X3D, 32 visible cores, AVX-512/VNNI                  |
| RAM                         | 94 GiB total                                                     |
| Model storage               | dedicated 500 GB local-ZFS disk, ext4 at `/var/lib/colibri`      |
| Colibri-shaped random reads | 7.39 GB/s, 19 MB O_DIRECT blocks                                 |
| Runtime placement           | 52 GB VRAM hot tier, 47 GB RAM warm tier, 273.9 GB SSD cold tier |

The NixOS and Proxmox declarations for the dedicated disk live in
<../../../../../nix/nixos/hosts/wyrm2/default.nix> and
<../../../../terraform/main/proxmox-vms.tf> respectively.

## Preflight and checkpoint notes

The 1.25 GB synthetic architecture fixture verified that the intended acceleration
path worked before committing to the full checkpoint. On GPU 0 it measured 3.39
tok/s CPU-streamed, 3.93 tok/s with dense CUDA, 17.95 tok/s with CUDA-pinned
experts, and 65.88 tok/s with pinned experts plus dense CUDA. GPU 1's first passes
were comparable. Later fixture repetitions slowed by roughly 2x while the
eight-worker checkpoint transfer was active, so storage-heavy benchmarks should
not overlap model downloads.

Colibri's cold path can read roughly 11.4 GB of experts per decoded token. The
dedicated filesystem measured 4.89 GB/s direct write and 7.39 GB/s over 5.1 GB of
19 MB O_DIRECT random reads (`iobench FILE 19 256 8 1`), equivalent to about 2.7
ms per block. <setup.sh> builds `c/iobench` alongside the CUDA engine so the gate
can be repeated against a checkpoint shard or an incompressible fixture.

The pinned checkpoint contains 150 root files totaling 383,760,077,466 bytes:
141 main shards, three MTP shards, and six metadata/tokenizer files. `coli plan`
can produce a plausible resource plan from a partial shard set, and `coli doctor`
validates shards that are present rather than proving repository completeness.
<verify_checkpoint.sh> therefore checks root file and shard counts, boundary shard
names, total byte count, MTP sizes, and absence of incomplete files before a run.

For the resumed Hugging Face Xet transfer, eight file workers and
`HF_XET_NUM_CONCURRENT_RANGE_GETS=8` completed the checkpoint in 47m27s. Sixteen
range requests per file collapsed throughput, while `HF_XET_HIGH_PERFORMANCE=1`
made no progress during a two-minute trial. <download.sh> preserves the productive
settings.

## Context capacity and numeric precision

The checkpoint config advertises `max_position_embeddings=1048576` with a RoPE
theta of 8,000,000. That is an architectural limit, not a practical capacity on
wyrm2. The pinned Colibri runtime stores its residual stream, compressed MLA KV,
DSA index KV, and attention workspaces as FP32. The INT4 expert weights and INT8
MTP head do not reduce context memory. Eligible CUDA expert kernels quantize an
activation row internally for their matrix multiply, then return FP32 output.

For this checkpoint, one sequence slot costs:

- 182,016 bytes/token for 79 layers of compressed MLA KV
  (`(512 kv_lora + 64 rope) * 4` bytes per layer);
- another 10,752 bytes/token when the 21 full DSA indexers are active; and
- a conservative 114,688 bytes/token attention-reconstruction reserve.

That is a 307,456-byte/token context-related safety slope. With the experiment's
64 GB RAM budget, the C runtime's safety calculation leaves approximately:

| Context | Context-related reserve | RAM expert slots/layer |
| ------: | ----------------------: | ---------------------: |
|      4K |                  1.3 GB |                     31 |
|     32K |                 10.1 GB |                     25 |
|     64K |                 20.1 GB |                     19 |
|    128K |                 40.3 GB |                      5 |
|    152K |                 46.7 GB |                      1 |

Only 4K was executed in this run. A 64K context should fit without eliminating
the RAM expert tier; 128K is the largest credible next experiment, but its much
smaller warm tier will likely reduce the expert hit rate and decode speed. Around
152K exhausts the current 64 GB budget's useful expert cache. An aggressive 80 GB
process budget moves that edge to roughly 204K but leaves little host headroom.
The advertised 1M context would require roughly 340 GB of RAM even with only one
RAM expert slot per layer. Each additional server KV slot adds about 25.3 GB at
128K because sequence KV state is independent.

The pinned `resource_plan.py` estimate is slightly optimistic at long context: it
counts MLA KV and the attention workspace but omits DSA index KV, or 10,752 bytes
per token when the indexer is active. The C runtime's cache-cap calculation does
include that state; use the values above when sizing this exact revision.

Long context also changes the compute path. DSA begins selecting at most 2,048
keys after position 2,048, but selected DSA attention disables Colibri's absorbed
CUDA-attention path for those layers. The CUDA kernel itself rejects contexts over
4,096 tokens, so all attention beyond 4K uses the CPU path even while expert and
dense CUDA tiers remain enabled. Long-context prefill and decode were not
benchmarked here.

There is no FP16 or BF16 KV/activation switch in this revision. Such a change
could recover about 20 GB of the 128K reservation, but would require an upstream
storage/kernel implementation and new correctness validation.

## Results

All rows use the same prompt, greedy token sampling, 4096-token context, and a
32-token output. Runs after the cold pass use `--auto-tier --vram 52` and refine
the learned expert placement in sequence.

| Run          | Expert policy      | MTP     |         Decode | Expert hit rate | Key profile result                                  |
| ------------ | ------------------ | ------- | -------------: | --------------: | --------------------------------------------------- |
| Cold         | all routed experts | off     |     0.17 tok/s |           48.6% | 87.7 s disk, 75.5 s expert matmul                   |
| Profiled     | all routed experts | off     |     0.26 tok/s |           77.5% | 32.0 s disk, 56.0 s expert matmul                   |
| Refined warm | all routed experts | off     | **0.28 tok/s** |           83.5% | 26.4 s disk, 50.7 s expert matmul                   |
| Refined warm | all routed experts | depth 3 |     0.27 tok/s |           76.5% | 73% draft acceptance; more expert loads             |
| Refined warm | expert top-p 0.7   | off     |     0.37 tok/s |           80.8% | 494.8 vs 862.5 expert loads/token; quality tradeoff |

MTP reduced main-model forwards, but verification touched a larger union of
experts: 1,010.2 expert loads per emitted token rather than 862.5, erasing the
speculative-decoding benefit in this storage hierarchy. Expert top-p was faster,
but knowingly discards low-weight routed experts and still missed the gate. That
top-p run also exposed a placement imbalance: only 36.2 GB of experts landed in
VRAM, while RSS rose to 70.5 GB and left about 10 GB of host memory free.

## Correctness and caveats

- The CUDA q8, q4, q2, and f32 correctness fixtures passed on both GPUs.
- Full-model outputs were coherent across cold, warm, MTP, and expert-top-p runs.
- The generated tiny CPU oracle matched only 25/32 positions and differed from
  the oracle committed upstream. Treat that as an upstream harness warning; this
  run does not claim bit-exact tiny-model reproduction.
- The fixture patch only adapts the benchmark parser to Colibri's newer
  `service / wait` profile line. Its legacy `disk` column records wait time.
- The observed measurements predate any stable LiteLLM service. No cluster route
  was created because full-quality throughput failed the stated gate.

## Deferred serving checks

At the pinned Colibri revision, all 36 upstream OpenAI-server tests passed from the
`c/` module root. They cover authentication, model listing, chat completions,
streaming, request queues, and tool-call declaration, parsing, and streaming. This
establishes the server contract in isolation; a real-model forced-tool request was
not run, so function calling is not end-to-end validated.

A temporary listener on wyrm2's Kubernetes node address was reachable from a live
cluster LiteLLM pod. If this experiment is revisited, run an authenticated Colibri
listener and expose it through a selectorless Service and EndpointSlice; preserve
`/run/opengl-driver/lib` in the runtime library path. A Kubernetes version should
use SSD-backed storage rather than the current HDD-backed Ollama model PVC.
