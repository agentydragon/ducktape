# Rugged speed probes after connecting AC power

Results and interpretation belong in [rugged_local_llm.md](../../rugged_local_llm.md).
This run repeats the [battery-phase measurement script](../2026-10-13_rugged_repeated_speed/measure.sh)
with the same cached model digests, prompts, 32,768 context, 256-token output cap,
and three repetitions per prompt size. The output directory is separate because
power conditions changed. `ac-online.txt` and each request's power record confirm
AC status; `power-profile.txt` records the unchanged balanced profile.

The temporary Ollama 0.40.2 server listens on loopback port 11500, with Vulkan,
integrated GPUs enabled, cloud features disabled, one parallel request and one
loaded model. `server.log` preserves device selection, GPU buffer allocations,
cache behavior, and native timing logs. Models are unloaded between candidates.
This is an unloaded-model initialization, not a cleared operating-system cache.

`host-vmstat.txt` records concurrent host load. Other workstation sessions were
active, including periods of swapping; these are not isolated maximum-throughput
measurements. Interpret ranges and power conditions with the medians. Model
residency from `/api/ps` is not peak physical memory; Gemma's `size_vram` reporting
is unreliable. See the battery run's README for metric definitions and caveats.
