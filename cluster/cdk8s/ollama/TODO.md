# TODO — local inference on wyrm2

## Checkpoint provisioning

- Make TabbyAPI checkpoint staging declarative. Pin the upstream revision and file
  digests for each checkpoint, including the abliterated build. Resume incomplete
  downloads and publish each verified checkpoint to its own directory before
  GitOps points the serving workload at it. Keep the previous checkpoint available
  for rollback. See the [current staging procedure](README.md#tabbyapi-checkpoint-staging).
- Evaluate [Praesto](https://github.com/federicolepera/praesto) for node-local
  TabbyAPI staging on `wyrm2`. Check whether it can resume a full-size pinned
  EXL3 download and verify upstream digests. Confirm how to retain rollback
  checkpoints across cache eviction and `ModelCache` deletion.

## TabbyAPI context and prefix caching

Use the [stock Qwen 256K run](../../docs/inference/runs/2026-11-18_dual_5090_exl3/README.md#live-256k-check-after-rebalancing-the-gpus)
as the baseline. Its cold prompts took 36–37 s to first visible text and
left at least 6,491 MiB free on GPU 0 and 4,785 MiB on GPU 1, including
desktop usage. Record per-GPU peaks, latency, throughput, and cache hits
in the inference run record.

- **More warm windows.** Keep one active generation and
  `max_seq_len: 262144`. Try `cache_size: 532480` at Q8 for two 256K windows
  plus the current 8K spare. The extra K/V, sparse index, and MTP tensors
  are estimated at 4.47 GiB across both GPUs. Alternate two distinct
  near-limit threads and check that both prefixes stay warm and each
  card retains prefill headroom.
- **Simultaneous generations.** Test `max_batch_size: 2`, including
  same-checkpoint streams and switches while streams are active. Compare two
  overlapping cold prefills and a short
  request arriving during a long run against the current serial queue.
  Measure per-request latency, aggregate throughput, and peak VRAM.
- **Longer single window.** Try a 512K pool and sequence cap as a separate
  experiment beyond the checkpoint's native 262,144 tokens. Evaluate
  retrieval at several positions and a long-context tool round trip
  against the native-context baseline, alongside prefill latency and VRAM.
- **Measure prefix-cache writeback overhead.** Use the
  [211K-prefix results](../../docs/inference/runs/2026-11-19_tabby_prefix_cache/README.md)
  for the current RAM and NVMe tiers, including the replay after pod replacement.
  Compare RAM alone with both tiers enabled to isolate writeback overhead.
  See the [experiment runbook](prefix-cache-probe.md).
