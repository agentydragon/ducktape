# Repeated rugged Ollama speed probes

Results and interpretation belong in [rugged_local_llm.md](../../rugged_local_llm.md).
This directory retains requests, native responses, model digests, and measurements.

`measure.sh BASE_URL MODEL OUTPUT_DIRECTORY` runs an unloaded-model initialization
request, then three requests at each of two synthetic prompt lengths. Each request
uses 32,768 context, a 256-token output cap, temperature zero, and disabled thinking.
Input is a rotated fixed vocabulary followed by the same coding instruction. The
changed prefix reduces reuse between measured requests; actual prompt token counts
and processed-token durations are preserved rather than inferred from word counts.
The initialization request is excluded from warm throughput summaries.

Prefill and decode rates use Ollama's native token counts and nanosecond durations.
Total duration includes prefill and generation; it is not streaming time to first
token. Early end-of-sequence outputs retain their actual token counts. These are
speed probes, not coding-quality evaluations.

`/api/ps` records model residency, not peak physical memory. `/proc/meminfo`
snapshots describe the whole host before and after each request and cannot isolate
the accelerator allocation. In particular, Gemma's reported `size_vram` is known
to be unreliable. Do not compare these values directly with NPU process RSS or
interpret before/after snapshots as a peak-memory measurement.

The host started on battery in the balanced power profile with powersave CPU
governors. Per-request power records and UTC start times allow changes to be
identified. The temporary server uses its own model cache and a single loaded
model/parallel slot; inference runs are serialized with the NPU and coding
evaluation lanes. The scripts do not install or activate host configuration.

The initial `qwen3-4b-contended` batch overlapped a repository-wide pre-commit
run and first-time EvalPlus oracle/scoring work. CPU pressure reached about 70%
and memory pressure about 9%; its three short-prompt samples are retained but
excluded from the main comparison. The first longer request was deliberately
cancelled, leaving an empty response file and no valid metrics. After stopping
our competing checks, the complete suite restarted in the per-model directories.
Other user workloads remained active. `host-vmstat.txt` records host CPU, memory,
swap and I/O activity during the restarted suite; this is a working laptop, not
a quiescent benchmark host. Battery operation continues at the user's request.
