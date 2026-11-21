# TabbyAPI prefix-cache probe on wyrm2

Compare the current one-request server with an 8 GiB pinned RAM tier, then
with the same RAM tier plus a 16 GiB NVMe cache. Keep the model and draft
quants, 270,336-token GPU pool, 262,144-token request window, and GPU split
fixed. Use synthetic prompts throughout the probe window. Disk pages store
prompt token IDs and recurrent state. Use a separate directory from the
read-only model snapshots.

## Prepare the GitOps rollout

Schedule a GPU window with TabbyAPI users. This changes the pod template
and recreates the only TabbyAPI replica. The previous uncached startup
took 218 seconds. Before applying the cache change, confirm 16 GiB plus
filesystem headroom on `/games` and create the local PV directory on
`wyrm2` with `install -d -m 0700 /games/tabbyapi-prefix-cache`.
For a directory owned by the host operator, grant the capability-dropped
TabbyAPI process access with `setfacl -m u:0:rwx /games/tabbyapi-prefix-cache`.
The 16 GiB bound is enforced by
`memory.disk_cache_size`. The local PV capacity is a binding declaration.

Use GitOps for each arm. Start with the currently deployed config. For
the RAM-only arm, apply the cache change with `memory.disk_cache_dir`
omitted. For the NVMe arm, include it. Reloader restarts the pod when
its ConfigMap changes. For each rollout, verify the revision, new pod
readiness, the bound claim, and cache initialization in TabbyAPI logs.
A GitOps revert restores the original server. Retain only the synthetic
cache artifacts needed for the experiment.

## Measure

Port-forward the private TabbyAPI service from an authorized shell
(`kubectl -n ollama port-forward svc/tabbyapi 15000:5000`). Use the same
synthetic corpus shape as the [256K baseline](../../docs/inference/runs/2026-11-18_dual_5090_exl3/README.md#live-256k-check-after-rebalancing-the-gpus).
Capture the exact JSON request for each prompt and verify token counts
from server usage. Include a distinct tag near the start for each arm
and each unrelated eviction prompt. Reuse the identical saved body
when revisiting a prefix. Send through `/v1/chat/completions` with
`stream: true`, `stream_options: {"include_usage": true}`, a short
generation budget, and a fixed temperature.

1. **GPU baseline.** On stock `qwen38-exl3-268`, send synthetic prompt A
   at about 192K tokens and repeat it while warm. Send two distinct
   comparably long prompts on stock to evict A, then send A again.
   Separately run stock A → a short request on
   `qwen38-exl3-250-abliterated` → stock A. Verify which checkpoint is
   loaded after each request with `GET /v1/model`.
2. **RAM tier.** Repeat the same-checkpoint eviction sequence. Capture
   `/v1/status` before and after replay and look for tier promotion with
   `cache.alloc_tier_pages` increasing and `cache.alloc_disk_pages` at
   zero. RAM is freed on checkpoint unload.
3. **NVMe tier.** Repeat the checkpoint switch sequence with a fresh A.
   Wait for idle flush and check `cache.disk_cached_tokens` before
   leaving stock. On return, require a material cached-token hit and
   disk promotion in the new stock generator's `cache.alloc_disk_pages`.
   Repeat A after a GitOps pod restart to check persistence across
   processes.

Record streamed `usage.prompt_tokens_details.cached_tokens` and native
`timings.prompt_ms` from the terminal usage event. Save the first
nonempty text delta time separately from HTTP header arrival. For
synthetic requests, `curl --no-buffer --trace-time --trace-ascii` can
capture that timestamp. Use TabbyAPI unload, load, and warmup timestamps
to separate checkpoint switching from prefix restore and prefill.
Record per-card VRAM, pod memory, and cache disk use. Compare A→B too,
since persisting A during unload can add time to that request.
