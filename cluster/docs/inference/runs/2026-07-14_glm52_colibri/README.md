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
- **Detailed probes, evidence, and stopping criteria:** <epistemic_state.md>

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
but knowingly discards low-weight routed experts and still missed the gate.

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
