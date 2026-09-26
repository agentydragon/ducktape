# Qwen3.8 Flash Next Q4: one Terminal-Bench pass with two compactions

September 26, 2026, wyrm2. **`interleaved-vigenere` passed: reward 1.0,
six verifier tests passed, no Harbor exception.** Terminus-2 completed two natural
compactions and resumed tool use after each. This establishes one long task's
completion with this local configuration; it is not a suite score or evidence of
equivalence to a cloud model. The task was the first entry in the previously frozen
[CPU12 selection](../2026-09-26_harbor_review/cpu12_tasks.json), not chosen after
seeing its result. There was one whole-task attempt.

## Configuration and evidence

The unchanged [queue recipe](../2026-09-26_qwen38_queue/README.md) ran from copied
source revision `e6235501337533658a33e89319b1f8813d458965`: UD-Q4_K_XL from SSD,
131,072 context, Q8_0 K/V, two RTX 5090s in layer-split mode, one inference slot,
xhigh reasoning, temperature 0.6, 32,768 output tokens per request. Harbor 0.23.0,
Terminus-2 2.0.0; llama.cpp build 11151, commit
`bd4f514db14d87fded667787a7a963bfbaa98e89`. Task limits were unchanged: eight hours
for the agent, fifteen minutes for verification, 4 CPUs and 4 GiB per environment.

[metrics.json](metrics.json) records exact timing/token totals, compaction boundaries,
source revision, task checksum, and SHA256 hashes of source artifacts.
[Verifier output](verifier-stdout.txt) and the [CTRF report](verifier-ctrf.json) are
committed. Full raw artifacts, including the generated solution, main trajectory,
six summarization trajectories, server log and resolved config, are retained locally
under `/tmp/wyrm2-qwen38-q4-20260926/`, with a 2.1 MiB compressed copy outside `/tmp`:
`/var/lib/llm-models-ssd/experiment-artifacts/wyrm2-qwen38-q4-20260926.tar.gz`.
Archive SHA256: `81c9ab2196e7c5bb58d17315990e3fbd87e287138880de404c61f931a8c4ed83`.
The committed recipe and result summary are separate from the full local trace.
Do not feed the solution/trace into future benchmark attempts.

## Outcome and cost

All times below are Pacific on September 26.

| Measurement                                                        |                                     Result |
| ------------------------------------------------------------------ | -----------------------------------------: |
| Agent execution                                                    |              03:46:29–11:26:09; 7h 39m 40s |
| Harbor job, including setup and verification                       |              03:46:24–11:27:02; 7h 40m 38s |
| Agent deadline margin                                              |                                    20m 20s |
| Verifier phase                                                     | 41.78s; pytest reported 6 passed in 29.58s |
| Agent turns / bash command calls                                   |                                  154 / 246 |
| Successful compactions                                             |                                          2 |
| Harbor input / cached input / output tokens                        |            6,208,823 / 5,833,621 / 450,774 |
| Native server completed requests                                   |                                        161 |
| Native prompt evaluation                                           |          50m 17s; 377,374 evaluated tokens |
| Native generation                                                  |       5h 25m 48s; 483,542 generated tokens |
| Native model time / agent wall time                                |              6h 16m 5s / 7h 39m 40s; 81.8% |
| Remaining wall time: commands, terminal waits and harness overhead |                          1h 23m 35s; 18.2% |
| Aggregate decode rate                                              |                             24.74 tokens/s |
| Largest observed request prompt, including summary calls           |   81,541 tokens; 62.2% of physical context |

Native timings sum each request's `prompt eval time`, `eval time`, and `total time`
from `server.log`; decode rate is total generated tokens divided by total generation
seconds, not the mean of per-request rates. Remaining wall time subtracts summed
native model time from `result.json.agent_execution` duration. It is **not measured
command CPU time**. Requested command waits sum to 4,967.8 seconds, consistent with
most of that residual being terminal waits. Input totals include repeated history;
they are not unique context size.

Harbor's recorded output is 32,768 tokens below native server output. The trial log
contains one output-limit truncation: those generated tokens were spent but omitted
from Harbor's accepted-response totals. Native requests are 154 main turns plus six
summary/Q&A calls plus that truncated request. The longest generation took 22m 44s.
The earlier two-hour snapshot was 98.4% inference; it does not describe the full run,
which later spent substantially more time on commands/waits.

## Compaction evidence

| Completed handoff | Main trajectory step | Previous action's prompt | First resumed action's prompt |
| ----------------- | -------------------: | -----------------------: | ----------------------------: |
| 07:32:20          |                   48 |                   72,765 |                        10,676 |
| 09:12:08          |                   86 |                   70,880 |                         8,110 |

Both markers have `extra.context_management = {"type":"compaction","boundary":"replace"}`
and references to saved summary, question and answer trajectories. Subsequent action
steps execute commands, and the final verifier passes. These are actual handoffs,
not just a configured flag or an incremented counter. The before/after figures are
adjacent ordinary action requests, not identical payloads before and after compression.
The conservative tokenizer-based trigger and 32K output reserve mean there was no
need to fill the physical 128K window. No context-overflow exception was recorded.

## Resource observations and limitations

At server readiness, available host RAM was 58.4 GiB, free desktop GPU memory
8,186 MiB, and free second-GPU memory 2,098 MiB. These are snapshots, not observed
minima over the run. Startup logged GPU model buffers of 19,714 and 26,991 MiB,
1,836 MiB total KV buffers and 112.57 MiB recurrent state. CPU-mapped buffers and
lazy loading remain part of this configuration; the layer-offload log does not mean
all model bytes fit in VRAM.

Downloads overlapped the first hour: Q5_K_XL and IQ4_XS completed size/hash verification
at 04:48:50. Storage/runtime effects and that overlap prevent a clean quant-speed
comparison. Model SSD free space was about 150 GiB at 12:12; no additional disk was
needed for these downloads.

The wrapper exited with `resource_or_service_guard` at **11:27:28**, after Harbor
saved the passing result at **11:27:02**. Logs show repeated Kubernetes API/DNS
timeouts during the Ollama-paused check. This is a post-result service-check failure,
not a model failure or an agent timeout. The server was cleaned up. Harbor also
printed an unclosed aiohttp-session warning after its summary. Preserve both facts:
the verified task passed, but process cleanup did not end cleanly. A follow-up should
bound the whole API-check duration and avoid classifying an already completed task
as interrupted; it must retain the desktop and Ollama guards.

## Next comparison

At 12:10:17, the existing serial launcher started IQ4_XS on the same task with 128K,
Q8 KV and the same agent settings. Source revision:
`6411aede31e98e0087ac3d78caf775b603f1fd01`; output:
`/tmp/wyrm2-qwen38-iq4-20260926/`. Four completed agent turns were observed by 12:15;
no IQ4 result is claimed here. Its server RAM cap
is 24 GiB versus Q4's 34 GiB, and downloads are finished, so elapsed time compares
these practical configurations rather than isolating weight quant alone.

After IQ4, compare completion, compaction recovery, token use and elapsed time before
choosing another predetermined task or practical OpenCode task. Q4 remains the known
passing control. Native 256K and supported smaller KV formats address capacity;
Q5 addresses precision. Keep those changes separate and all inference serial. A
single matched pair can guide the next experiment but cannot establish a quality delta.
