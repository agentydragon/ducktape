# Context, concurrency and precision after the SSD rollout

September 27, 2026. Predictions below are capacity arithmetic, not additional inference results.
[Serving measurements](README.md) establish IQ4_XS/Q8 at one 128K or 256K slot. No concurrent inference or context above
256K was run.

## Two 128K windows

Likely feasible with shared weights: two Q8-cache 128K sequences need about 3.806 GiB of persistent state, versus 3.696
GiB for one 256K sequence. The extra 0.110 GiB is another recurrent state. Runtime workspace, checkpoints and scheduling
overhead remain additional. This is not two model processes or one model per GPU: both GPUs already contribute to the
single loaded model.

Ollama currently has `OLLAMA_NUM_PARALLEL=1`. A controlled two-slot experiment would set it to two with a 131,072-token
per-request context and verify the runner's total 262,144 context and per-slot 131,072 limit. The
[0.34.4 scheduler](https://github.com/ollama/ollama/blob/v0.34.4/server/sched.go) multiplies effective per-sequence
context by the parallel count; its architecture denylist does not name this model's `qwen4exp` family. That absence is
not a runtime correctness test. Verify that two requests actually overlap and do not just queue.

Batching may improve aggregate tokens/s while reducing each conversation's speed; CPU experts, SSD lookups and PCIe
traffic are shared bottlenecks. Two GPUs do not imply two conversations at today's single-conversation speed. Long
prefill may also delay the other conversation. Measure time to first token, per-stream decode, aggregate throughput and
peak memory at short and substantially filled histories.

## Larger windows and the cache slope

The [model card](https://huggingface.co/Qwen/Qwen3.8-Flash-Next#best-practices) specifies native 262,144 context and
YaRN extension: factor 2 for 524,288, factor 4 for roughly 1M. Static scaling may affect short-context quality. Its
recipes name vLLM, SGLang and TokenSpeed; they do not prove this GGUF/llama.cpp path implements the architecture's
scaling correctly.

Ollama 0.34.4's `effectiveContext` clamps requested context to GGUF metadata's training context. Merely creating a 512K
alias would therefore not establish 512K serving. First verify an explicit extension path in a separate llama.cpp
experiment or supported runtime; do not change checkpoint metadata just to evade the cap.

The [header/source derivation](../2026-09-26_qwen38_capacity/README.md) includes the attention indexer and quantization
block overhead. Persistent attention cache in GiB, excluding 0.110 GiB recurrent state per sequence and temporary
workspace:

| K / V cache      |  128K |  256K |  512K | 1,048,576 |
| ---------------- | ----: | ----: | ----: | --------: |
| Q8 / Q8, current | 1.793 | 3.586 | 7.172 |    14.344 |
| Q8 / Q4_0        | 1.418 | 2.836 | 5.672 |    11.344 |
| Q5_0 / Q5_0      | 1.160 | 2.320 | 4.641 |     9.281 |
| Q4_0 / Q4_0      | 0.949 | 1.898 | 3.797 |     7.594 |

At 256K, Q8-to-Q4 saves 1.688 GiB; at 512K it saves 3.375 GiB. A fixed cache budget holds about 1.89 times as many
tokens with Q4, before recurrent/workspace overhead. Two 256K windows cost 7.392 GiB with Q8 versus 4.017 with Q4; four
128K windows cost 7.612 versus 4.237. These are not promises of useful four-agent performance. Ollama's adapter passes
the same cache type for K and V. Mixed cache and the wider Q5 sweep belong in direct llama.cpp, checking actual kernel
support and temporary F16 buffers rather than trusting only the accepted flag.

512K/Q8 adds 3.586 GiB over today's 256K/Q8. With current weights this can displace GPU-resident weights into CPU memory
rather than requiring a new GPU, but costs latency and host RAM. At 1M the extra Q8 cache is 10.758 GiB. Memory
feasibility, scaling correctness and retained long-context reasoning are three separate tests.

## Weight precision is the larger lever

The deployed 256K model has about 46.35 GiB of GPU weight buffers versus 3.586 GiB of attention cache. SSD lookup
embeddings and other CPU-mapped weights account for the rest of the checkpoint. From pinned
[artifact/header sizes](../2026-09-26_qwen38_capacity/weight_sizes.json):

| Weight quant    | Total files, GiB | Non-PLE weights/headers, GiB | Reduction from IQ4_XS, GiB | Published mean KLD |
| --------------- | ---------------: | ---------------------------: | -------------------------: | -----------------: |
| IQ4_XS, current |            87.25 |                        60.43 |                          0 |             0.0836 |
| Q3_K_XL         |            83.81 |                        56.98 |                       3.44 |             0.1065 |
| IQ3_XXS         |            76.33 |                        49.51 |                      10.92 |             0.1651 |
| Q2_K_XL         |            73.45 |                        46.63 |                      13.80 |             0.2246 |
| IQ1_S           |            67.56 |                        40.74 |                      19.69 |             0.3961 |

All these retain a 26.82 GiB PLE lookup table. Artifact savings are not automatically free VRAM: autofit may use them to
reduce CPU offload while filling the same GPU budget. IQ3's 49.51 GiB non-PLE payload is an interesting candidate for
approaching GPU-resident compute weights, especially at 128K; placement granularity and buffers still need measurement.
A smaller quant may improve latency through less CPU traffic, but different kernels mean speed is not monotonic in file
size.

Q3 is a modest capacity gain with less published distortion. IQ3 is the more useful capacity experiment. Q2 saves only
another 2.88 GiB over IQ3 while increasing distortion substantially, so it is lower priority for this capability-first
program.

Activations are temporary computation buffers, distinct from persistent KV. There is no generic four-bit-activations
switch for this GGUF deployment. Reducing batch or microbatch size is a workspace experiment that can trade prefill
speed for headroom. NVFP4 weight/activation inference is a separate format/runtime project, not a drop-in quantization
flag for the current model.

## What quality evidence actually exists

- [Unsloth's exact-quant comparison](https://unsloth.ai/docs/models/qwen3.8-next#quantization-analysis) supplies the KLD
  figures above and top-1 recovery: IQ4 89.554, Q3 88.315, IQ3 85.414, Q2 82.715. These measure distribution fidelity,
  not coding pass rates. They do not imply that IQ3 loses four percentage points of agent success.
- [DevSnack's exact IQ4_XS custom suite](https://devsnack-blog.vercel.app/benchmarks/models/qwen3-8-flash-next) reports
  coding 10/12, tool calls 10/15, single-agent 4/12 and multi-agent 4/10. Its command uses GB10, F16 KV,
  `--ctx-size 8192 --parallel 8` and automatic reasoning; the page does not establish effective per-slot context or
  provide enough task and evaluator detail to reproduce those scores. No smaller-quant comparison is shown.
- Our matched Q4/IQ4 Terminus attempts both passed the same six verifier checks. Q4 finished; IQ4 exhausted its
  eight-hour agent budget while still testing. One task is useful reproduction evidence, not a capability equivalence
  estimate.
- This bounded search found no controlled agentic/coding comparison of these GGUF weight quants, nor model-specific
  long-context agent scores for Q8 versus Q4 KV. Published full-model scores and NVIDIA NVFP4 results do not fill those
  gaps.

Q8 KV remains the control. Q5 or Q8-K/Q4-V are intermediate experiments; Q4-K/Q4-V offers greater capacity with
unresolved quality risk. Neither "harmless" nor "crippling" is established. Keys and the indexer affect which history is
attended to, so short prompts alone cannot validate the lower-precision cache.

## Next experiments, conditional on results

The operator expects 2/0 GiB of desktop allowance to suffice. The tested 4/2 fit targets address runtime allocation as
well: at 2/0, GPU1 had only 96 MiB free and long prefill OOMed. They are not a new desktop requirement. Test 2/2 targets
or smaller prefill batches to reduce the working margin, repeating the same long input and recording peak usage. Do not
conflate a successful short decode with safe prefill.

1. Retain IQ4/Q8 serving as the baseline. For a deliberate concurrency experiment, test two 128K slots with different
   prompts/markers and substantial independent histories; compare against the same requests serially. Keep this separate
   from the serial eval queue. Revert to one slot afterward. The existing instruction against concurrent evals remains
   in force; no two-slot run has been started.
2. With one slot, compare IQ4/Q8 against Q4 KV at 128K/256K. Add Q5 or mixed K/V if Q4 degrades or a smaller memory
   saving suffices. Test retrieval across depths, earlier-fact-dependent edits, tools, and matched coding tasks. Verify
   compaction policy and effective context before spending hours on a trajectory.
3. Compare IQ3_XXS/Q8 against IQ4_XS/Q8 on the same predetermined tasks. Record verifier rewards, explicit completion
   versus timeouts, reasoning/output tokens, prefill/decode latency and host/GPU memory. Repeat borderline outcomes. Use
   Q3 if IQ3's quality loss outweighs its capacity gain; retain Q4/Q5 as quality controls.
4. Attempt 512K only after verifying scaling implementation and native-window quality. Fill beyond 256K and test distant
   dependencies, not just allocation. Try 1M only if 512K is useful. MTP is a separate latency experiment after these
   controls, measuring acceptance and actual wall-clock gains.

The SSD has 150 GiB free at this check. IQ3's 76.33 GiB download physically fits, but would cross the earlier download
queue's 128 GiB free-space floor. Move the unused 147.42 GiB Q5 checkpoint to bulk storage before another large download
if retaining that floor; user authorization to move unused models already exists. No extra download or bulk copy was
started during the serving measurements.
