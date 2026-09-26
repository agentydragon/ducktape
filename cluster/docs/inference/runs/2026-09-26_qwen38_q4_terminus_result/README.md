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
`/tmp/wyrm2-qwen38-iq4-20260926/`. No IQ4 task result is claimed here. Its server RAM cap
is 24 GiB versus Q4's 34 GiB, and downloads are finished, so elapsed time compares
these practical configurations rather than isolating weight quant alone.

### Initial IQ4 speed and placement snapshot

At **12:20:24 Pacific**, six requests had completed and the seventh was generating.
[Snapshot data](iq4-initial-snapshot.json) preserves each completed request's prompt
size, native token counts/times, aggregate calculations and source-log hashes.

| First six completed requests         |       IQ4_XS |     Q4_K_XL |
| ------------------------------------ | -----------: | ----------: |
| Generation, aggregate tokens/s       |        43.44 |       28.67 |
| Prompt evaluation, uncached tokens/s |       346.79 |      114.49 |
| Generated tokens                     |       13,792 |      11,118 |
| Generation seconds                   |       317.48 |      387.81 |
| Completed request prompt range       | 1,189–10,730 | 1,191–8,176 |

IQ4's in-flight seventh request was at 42.80 generated tokens/s after 6,579 tokens.
The completed-request decode ratio is **1.52x**, but requests differ in content,
context and output length. Q4's early run overlapped downloads; IQ4's did not.
These numbers show preliminary serving throughput, not a task-time or quality gain.

| Size / startup allocation             |    IQ4_XS |    Q4_K_XL |
| ------------------------------------- | --------: | ---------: |
| Model files                           | 87.25 GiB | 103.69 GiB |
| GPU model buffers, both GPUs combined | 45.67 GiB |  45.61 GiB |
| CPU-mapped model buffers              | 42.46 GiB |  60.48 GiB |

IQ4 saves **16.44 GiB (15.9%)** in model files. The runtime fills approximately the
same GPU model budget and reduces host-side model buffers. Both configurations keep
the 26.82 GiB per-layer embedding table CPU-mapped with lazy SSD reads. CPU-mapped
size is a virtual mapping, not measured resident RAM; allocator buffer sizes are
not an exact partition of on-disk file sizes. KV/workspace allocations are additional.
The startup message `offloaded 49/49 layers to GPU` does not establish complete GPU
residency: the same log reports CPU tensor placement and mapped buffers. IQ4 remains
a GPU/host/SSD configuration. Its startup log is saved under the IQ4 artifact root.

After IQ4, compare completion, compaction recovery, token use and elapsed time before
choosing another predetermined task or practical OpenCode task. Q4 remains the known
passing control. Native 256K and supported smaller KV formats address capacity;
Q5 addresses precision. Keep those changes separate and all inference serial. A
single matched pair can guide the next experiment but cannot establish a quality delta.

### More aggressive weight quants, conditional on IQ4

The [pinned upstream inventory](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF/tree/38bb39ee97821de2c9009abb7e93950eec396e66)
also provides smaller quants. Sizes below come from our
[recorded file metadata](../2026-09-26_qwen38_capacity/weight_sizes.json).

| Quant      | Model files, GiB | Saving versus IQ4_XS, GiB |
| ---------- | ---------------: | ------------------------: |
| UD-Q3_K_XL |            83.81 |                      3.44 |
| UD-IQ3_XXS |            76.33 |                     10.92 |
| UD-Q2_K_XL |            73.45 |                     13.80 |
| UD-IQ1_M   |            69.42 |                     17.83 |
| UD-IQ1_S   |            67.56 |                     19.68 |

If IQ4 remains useful, IQ3_XXS is a reasonable next capacity/quality experiment:
its size reduction is more substantial than Q3_K_XL's. Q2 is a further step;
IQ1 variants are exploratory boundary tests with greater quality uncertainty.
No local quality-loss estimate is established for any of these quants. Preserve
Q4/IQ4 controls and test tool use and task completion, not just tokens/s.

All retain the same 26.82 GiB per-layer embedding table. Even IQ1_S exceeds total
VRAM as a complete model file; however its non-embedding portion is about 40.74 GiB,
which makes fitting that portion on the GPUs with lazy host-side embedding access
an interesting possibility. Runtime buffers, desktop reserves, kernel support and
actual placement must be checked; this is not a claim that it fits or remains useful.
No additional download or concurrent inference is scheduled by this note.

### External quant-specific evidence

Checked September 26, 2026. [Unsloth's current quantization analysis](https://unsloth.ai/docs/models/qwen3.8-next#quantization-analysis)
reports the following token-level statistics for these named quants:

| Quant      | Reported top-1 agreement, % | Mean KLD (lower is closer) |
| ---------- | --------------------------: | -------------------------: |
| UD-Q5_K_XL |                      93.680 |                   0.030415 |
| UD-Q4_K_XL |                      92.255 |                   0.046893 |
| UD-IQ4_XS  |                      89.554 |                   0.083630 |
| UD-Q3_K_XL |                      88.315 |                   0.106504 |
| UD-IQ3_XXS |                      85.414 |                   0.165120 |
| UD-Q2_K_XL |                      82.715 |                   0.224607 |
| UD-IQ1_M   |                      79.691 |                   0.314739 |
| UD-IQ1_S   |                      77.325 |                   0.396070 |

These measure token prediction/distribution fidelity, not task accuracy or retained
agent capability. IQ4's 2.701 percentage-point top-1 gap from Q4 does not mean a
2.701-point loss on Terminal-Bench. The displayed table does not fully specify the
evaluation corpus/runtime or establish a hash match to our pinned checkpoint.
Older third-party reproductions quote different values; use this dated primary-source
snapshot rather than mixing tables. No controlled large agentic benchmark comparison
of our two exact quants was found in this search.

Two primary reports offer limited task evidence:

- [DevSnack's DGX Spark IQ4_XS report](https://devsnack-blog.vercel.app/benchmarks/models/qwen3-8-flash-next)
  gives coding 10/12, tool calls 10/15, single-agent 4/12 and multi-agent 4/10 on
  its custom suites. It publishes `--ctx-size 8192 --parallel 8`, F16 KV and auto
  reasoning. Those settings and small denominators differ substantially from ours;
  the displayed command alone does not establish effective per-agent context.
- [Jose Romero's four-quant experiment](https://www.hijoseromero.com/benchmarks/qwen38-flash-next)
  compares IQ1_M, Q2_K_XL, Q3_K_XL and IQ4_XS on four one-shot coding/visual prompts,
  with raw outputs and browser checks. It uses 128K, medium thinking and an older
  Vulkan build. All four completed; IQ4 had a dashboard-rendering bug. The smaller
  IQ1 model spent more time thinking and took longer overall. This is a small practical
  demonstration, not evidence of equal quality or a direct Q4_K_XL comparison.

These support testing smaller quants, while leaving long-horizon quality, compaction
recovery and time-to-correct-completion to the local matched experiments.
