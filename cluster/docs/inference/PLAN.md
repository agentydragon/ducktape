# Wyrm2 capability-first inference plan (WIP)

Updated: 2026-09-26. The September 24 measurements and exact commands are in
[runs/2026-09-24_qwen38_ssd/README.md](runs/2026-09-24_qwen38_ssd/README.md).

## Objective and decision

User priority, verbatim: "The strongest model this machine can run, even if slow".
Also refresh releases since the last substantive inference experiments, July 17–18.
September Git edits mostly maintain paths and deployment wiring; they are not September model evaluations.

Choose a model, quantization, runtime, reasoning setting, and usable context together.
Optimize task success and independence from human rescue first; retain latency measurements to expose the cost.
No blanket tokens/s cutoff. A slow model earns its place by solving tasks the faster ones cannot.

## Initial machine snapshot (before storage work and experiments)

- Two RTX 5090s, 32,607 MiB each; driver 595.71.05. GPU0 used 2,460 MiB; GPU1 5 MiB.
- GPU P2P read support: `NS`; topology `PHB`. Do not assume two GPUs behave as one unified 64 GB allocation.
- Ryzen 9 9950X3D exposed through KVM, 32 vCPUs, AVX-512 available.
- `free -h`: 94 GiB total, 70 GiB available, no swap. This is a shared workstation and cluster node.
- Root filesystem after the operator ran `nix-collect-garbage`: 492G total, 425G used, 48G available, 90% used.
- `/var/lib/colibri`: 492G total, 447G used, 20G available, 96% used.
- At the post-GC recheck, node `Ready=True`, `DiskPressure=True`; `node.kubernetes.io/disk-pressure:NoSchedule` taint.
- Ollama Deployment `0/1`; replacement pod Pending/Unschedulable. Model PVC still Bound, 200Gi HDD class.
- No pods in `llm-bench`. Both GPUs respond; queried seven-day kernel log returned no matching Xid/reset entries.
  This does not establish stability under sustained inference or cover unavailable historical logs.
- Existing GLM-5.2 Colibri and DeepSeek-V4 IQ2 directories remain on disk; completeness not revalidated.

Reproduce with `nvidia-smi`, `nvidia-smi topo -m`, `nvidia-smi topo -p2p r`, `free -h`,
`df -h / /var/lib/colibri`, `kubectl get node wyrm2 -o json`, and `kubectl -n ollama get pods,deploy,pvc`.

## Existing evidence and its limits

Run paths below are relative to this directory. The July run records are historical evidence;
the September measurements below are separate from these historical results.

| Evidence                                                  | Finding                                                                                                                              | Limitation                                                                                                                                        |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| <runs/2026-07-17_e1_qwen3coder_awq/README.md>             | Qwen3-Coder-30B-A3B AWQ/vLLM TP2: 262K allocation, multi-depth needle passes, tool smoke passes; reported ~199 tok/s at nominal 128K | Coding task quality was external; warm-prefix and cold-prefill differ substantially                                                               |
| <runs/2026-07-17_e5_devstral_24b/README.md>               | Devstral Small 2: 128K allocation/needle passes; all three tool probes pass; ~90–96 tok/s at 8K/32K                                  | 128K latency request failed; summary table incorrectly locates its decode result at 128K                                                          |
| <runs/2026-07-17_e6_qwen36_35b/README.md>                 | Qwen3.6: 262K needle passes; tool probe fails                                                                                        | Hermes parser and limited reasoning budget confound conclusions about underlying model capability                                                 |
| <runs/2026-07-17_e7_gptoss120b/README.md>                 | gpt-oss-120b: ~12 tok/s with vLLM CPU offload; 16K configured; single/multiturn tools pass                                           | Not a 128K deployment or quality comparison; placement was not exhaustively optimized                                                             |
| <runs/2026-07-18_e9_deepseek_v4_flash_llamacpp/README.md> | DeepSeek-V4-Flash IQ2: 1.1 CPU / 2.9 Vulkan tok/s, 4K context configuration                                                          | GPU held attention while all experts stayed on CPU; expert placement sweep stopped at GPU lockup; no agent-quality result                         |
| <runs/2026-07-17_glm52_colibri_deepening/README.md>       | GLM-5.2 Colibri: ~0.15–0.16 tok/s warm in longer follow-up                                                                           | 64K allocation used a short prompt; not a filled-context result. Framework overhead was substantial, so storage alone is not an established cause |
| <runs/2026-04-29_swebench_n100_shuffled_gpt20/README.md>  | SWE-bench attempts aborted                                                                                                           | No usable N=100 capability result                                                                                                                 |

Other hubs: `x/local_llm/`, `x/benchmark_ollama/`, `props/docs/local_llm_evaluation/`,
and `cluster/docs/inference/model_comparison/`. Props local measurements were CPU-only in an older web environment,
not measurements of the two 5090s. Existing inference `PLAN.md` already favors lightweight run records over a new framework.

Audit cautions:

- E2's 1,000–1,500 tok/s gpt-oss numbers need remeasurement. The harness divides total reported completion tokens
  by time after first visible delta; unstreamed reasoning or buffered chunks could inflate that rate. This is a risk,
  not a proven explanation. Its nominal 128K input was actually 109,277 tokens, with null reasoning counts.
- E5 says it was the only parallel-tool success, but E1 also passed. A single weather probe is only a smoke test.
- A model card's full-precision quality score does not establish the quality of an IQ2 local deployment.
- A theoretical memory-bandwidth ceiling does not promise a corresponding attainable speedup.

## Refreshed candidates

All file sizes below are decimal GB, summed from quantizer file metadata, not peak RAM/VRAM requirements.
Runtime buffers, non-expert tensors, KV, page cache, the desktop, and temporary loading copies require headroom.

| Candidate              | Initial configuration to investigate                                                     | Why                                                                                                                       |
| ---------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| GLM-5.3-Flash          | CUDA llama.cpp compatible branch; UD-IQ3_XXS 120.37 GB; IQ2_XXS 101.84 GB fallback       | New 320B/18B-active candidate for the capability ceiling with GPU/RAM offload                                             |
| Qwen3.8-Flash-Next     | CUDA llama.cpp; UD-Q4_K_XL 111.33 GB, IQ4_XS 93.68 GB fallback                           | New model with 125B backbone plus 51B lookup embeddings and 4B MTP; investigate embedding offload separately from experts |
| DeepSeek-V4-Flash-0731 | CUDA llama.cpp; IQ2_M 90.93 GB, Q3_K_M 128.08 GB quality comparison if placement permits | New post-training relative to July preview, official agentic gains; closest continuation of E9                            |
| Qwen3.8-27B            | FP8 across the GPUs or a suitable GGUF                                                   | New resident quality/control point; compare a less compressed small model against aggressively compressed larger ones     |
| DeepSeek-V4.1-Flash    | JigSawPT `dsv41-porte` streaming fork with matching 502 GB checkpoint                    | Highest-priority ambitious feasibility track; new implementation makes a much larger model testable in principle          |
| MiniMax-M3             | IQ2_M 134.21 GB                                                                          | Already on old backlog; still untested. Bare 2-bit parameter arithmetic understated actual artifact size                  |

GLM-5.3-Flash upstream llama.cpp PR #27754 was OPEN/unmerged at inspection; pin a compatible implementation,
including its tool-template and long-context fixes. Qwen3.8's reported Blackwell SOFT_MAX issue #28403 was closed;
the reporter resolved that instance by rebuilding with a consistent CUDA toolchain. Neither fact proves a local run.

Primary sources:

- [GLM card](https://huggingface.co/zai-org/GLM-5.3-Flash),
  [quant files](https://huggingface.co/unsloth/GLM-5.3-Flash-GGUF/tree/main/UD-IQ3_XXS),
  [runtime PR](https://github.com/ggml-org/llama.cpp/pull/27754).
- [Qwen Flash card](https://huggingface.co/Qwen/Qwen3.8-Flash-Next),
  [quant files](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF/tree/main/UD-Q4_K_XL),
  [runtime guide](https://unsloth.ai/docs/models/qwen3.8-next),
  [resolved report](https://github.com/ggml-org/llama.cpp/issues/28403).
- [DeepSeek 0731 card](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731),
  [quant files](https://huggingface.co/unsloth/DeepSeek-V4-Flash-0731-GGUF/tree/main/UD-IQ2_M).
- [Qwen dense card](https://huggingface.co/Qwen/Qwen3.8-27B).
- [DeepSeek V4.1 card](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash),
  [matching streaming checkpoint](https://huggingface.co/JigSawPT/DeepSeek-V4.1-Flash-GGUF),
  [runtime author's report](https://github.com/JigSawPT/deepseek-v41-flash-on-5090).
- [MiniMax card](https://huggingface.co/MiniMaxAI/MiniMax-M3),
  [quantizer inventory](https://huggingface.co/unsloth/MiniMax-M3-GGUF/tree/main).

The V4.1 author reports ~5.1 tok/s on new content and ~21 on repeated content with one 5090,
125.7 GiB RAM and PCIe 5 NVMe. A September 23 direct-I/O update reports ~8.9–10.0 tok/s on first-pass prompts.
These are short-prompt, short-output external measurements. Wyrm2 has less RAM and a virtualized storage path;
two-GPU scaling, reasoning quality, sustained changing expert working sets, and long-context behavior are unknown.
Use the expanded VM model disk for the 502 GB checkpoint, archiving inactive checkpoints for working headroom.
Keep every expert; caching changes placement, whereas dropping experts changes the model.

## Storage recommendation

Subsequent September 24 inspection of Atlas confirmed that `virtio8` is
`local-zfs:vm-110-disk-7`, backed by the 4 TB Sabrent NVMe in `rpool`.
ZFS reported about 1.03 TB (956 GiB) available to datasets, shared with other VMs;
the pool had about 1.22 TB physically free. These are different accounting figures,
not additive capacity. The HDD share had about 27 TiB free.
Root free space had increased to 105 GiB by this later check; the earlier disk-pressure
observation above was not rechecked at that time.

| Storage                             | Current allocation | Proposed allocation | Reason                               |
| ----------------------------------- | ------------------ | ------------------- | ------------------------------------ |
| Bazel output/cache (`virtio4`)      | 150 GiB            | 250 GiB             | Only 4 GiB free at inspection        |
| Root (`scsi0`)                      | 500 GiB            | 500 GiB             | 105 GiB free after cleanup           |
| Games, containerd, Kubernetes disks | Existing           | Existing            | No expansion needed for this program |

The model-volume change was applied online on September 24:
**1,024 GiB at `/var/lib/llm-models-ssd`**, with about 515 GiB free and all existing
models retained. Implementation and verification are in
[PR #7893](https://github.com/agentydragon/ducktape/pull/7893).
The VM was not rebooted and its running system generation and critical service PIDs
were unchanged. The NixOS configuration was staged for the next normal boot;
only the reviewed mount-related files were applied to the running system.
Terraform currently ignores disk changes to protect legacy CSI attachments, so this
grow used a targeted Proxmox resize rather than bootstrap. The PR also updates
launcher defaults and the monitoring selector for the normal Flux rollout after merge.

The operator is comfortable archiving or deleting unused models. Candidate archives
are GLM-5.2 Colibri (358 GiB) and the July DeepSeek IQ2 checkpoint (86 GiB).
Confirm they are unused, copy to HDD and verify before removing the SSD copies;
retain small run records. With both archived, the 502 GB V4.1 checkpoint and three
comparison checkpoints total roughly 770 GiB, fitting the expanded model filesystem.
Use 400 GiB of pool-available SSD space as an operating headroom target, checking it
before downloads. Existing disks are thin-provisioned: shrinking an empty allocation
does not recover the nominal size as physical space.

## September 26 checkpoint and decision

**Treat Qwen3.8-Flash-Next as the working baseline and validate its local quality
with a bounded, meaningful Terminal-Bench subset.** Published scores justify
investigation, not local Q4 equivalence to a GPT/Claude effort level. Pair Q4 and
Q5 on the same tasks after checking context handling and safe placement; retain
single-session capacity measurements and exploration of alternative models.

**All inference experiments are strictly serial at the operator's request:** one
active task, one model configuration, one server slot, and no competing inference
requests. This includes preflight probes, subagent model calls and alternative-model
experiments. Verify exclusive use before starting; defer if interactive usage would
compete. Simultaneous-generation tests are deferred because they change KV-cache
pressure and would confound this evaluation.
The operator reports that Flash-Next **completed a long, real knowledge-management
task reasonably in OpenCode, without obvious bugs or breakages, across more than
128K cumulative context, at least one compaction and about four subagent calls**. This is a useful
end-to-end agent outcome, not just a favorable impression. It is operator-reported,
not an independently scored coding task. The operator-supplied
[OpenCode and server recipes](runs/2026-09-26_harbor_review/README.md) are captured;
effective client defaults and compaction trigger/reserve still need capture.
The reported compaction is evidence of working history management in that client;
it does not establish that Mini-SWE uses the same policy. Preserve OpenCode as the reference client and prioritize making that
working everyday/overnight workflow reliable. Use small checks to investigate
regressions and choose improvements. The interrupted Harbor benchmark is not a
reason to reject a model that already did useful work. The operator also finds it
substantially nicer than their GPT-OSS experience roughly a year earlier on the
same hardware. Preserve that observed practical advance; do not require another
benchmark campaign before accepting Qwen as the working baseline.

Completed here:

- Model SSD expanded to 1 TiB and renamed `/var/lib/llm-models-ssd` (#7893).
  Dense Q8 and all four Flash Q4 shards are downloaded and SHA256 verified.
- Dense Qwen3.8-27B Q8: about 51 tokens/s on GPU1, 50 with equal layer splitting;
  tensor splitting with a container-only NCCL update gives 68–72 tokens/s.
  Both GPUs demonstrably help dense decode with the right split.
- Flash-Next Q4: 32.73 tokens/s on the short prompt; at 24,132 actual input tokens,
  126.703 s fresh prefill and 30.72 tokens/s decode. One synthetic tool-result
  grounding failure was retained; seeded repeats were clean. These are smoke
  measurements, not a coding pass rate. See [results](results.md).
- The operator subsequently ran Harbor/mini-swe-agent, apparently with 128K
  configured context, and reported 1 success in 11 completed tasks after 11h49m.
  Configuration, errors, and partial-result interpretation belong in the
  [bounded Harbor audit](runs/2026-09-26_harbor_review/README.md). The saved job now
  has 12 completed trials, one pass and six context-overflow exits at a 131,072-token
  limit. It records 1,326,739 output tokens and about 98.1% of input tokens cached.
  At 30 tokens/s, that output alone takes about 12.3 hours: long reasoning/output
  trajectories can explain hour-scale tasks without a broken timeout. These are
  recorded harness counters, not independently validated server accounting.

Current state checked September 26: no host Docker experiment containers running;
Ollama was re-enabled by #8000 and now runs image `ollama/ollama:0.34.4`, context
default 131072, on a 350 GiB HDD PVC. The model SSD has about 384 GiB available;
host RAM about 30 GiB available while Ollama is loaded. An initial GPU snapshot
showed only about 1 GiB free on desktop GPU0. This is not evidence of a desktop
stall, but is less headroom than our experiment policy. Do not launch a competing
server. Pause Ollama by a focused GitOps PR immediately before exclusive experiments;
the operator has authorized this again. No reboot or full NixOS activation.

The reported ~1 token/s on HDD/Ollama versus ~30 on SSD/llama.cpp changes several
variables at once: storage, runtime version, model representation, placement, context,
and cache state. Treat it as an operational regression to isolate, not proof that
HDD or the older runtime alone explains all of it. #8041 records earlier Ollama
findings; those predate the currently deployed 0.34.4. Check current logs and the
actual loaded model/context, rather than only the environment default.

### What the Artificial Analysis comparison does and does not say

Live [AA leaderboard](https://artificialanalysis.ai/leaderboards/models) checked
September 26, index v4.3.2:

| Model              | Intelligence Index | Terminal-Bench 4.0 |
| ------------------ | -----------------: | -----------------: |
| Qwen3.8-Flash-Next |              39.82 |             25.25% |
| GPT-6 Sol low      |              33.90 |              9.09% |
| GPT-6 Sol medium   |              39.78 |             18.69% |

The aggregate comparison is approximately Sol medium. It is not a forecast of
local Q4 pass rate. AA's [Terminal-Bench methodology](https://artificialanalysis.ai/evaluations/terminalbench-4-0)
uses all 66 tasks, mini-swe-agent, and three repeats per task. Its detailed
[implementation](https://artificialanalysis.ai/methodology/intelligence-benchmarking)
specifies 500 steps, upstream task deadlines, and no transcript compaction.
Our partial run has
few outcomes, harness failures, a different serving configuration, and no matched
reasoning/output/time-budget record. Its early completion order is not a random
sample. Do not infer that larger context alone explains the difference, or declare
either equivalence or collapse from 1/11.

## Two complementary tracks

Maintain both Qwen improvement and alternative-model research, executing every
inference run serially. Do one bounded Qwen quality/capacity batch, then give the strongest credible alternative
a feasibility pass; do not exhaust Qwen's tuning space before exploring. This is
an experiment-allocation policy, not a claim that either model will win.

## Improve the working Qwen configuration

### 1. Measure usable context for one session

Reuse the demonstrated SSD/llama.cpp/OpenCode configuration for these measurements.
Capture effective settings and do only enough startup/tool checking to establish
that the reference is running. Ollama deployment and a new coding harness are not
prerequisites.

The [official model card](https://huggingface.co/Qwen/Qwen3.8-Flash-Next) specifies
262,144 native total tokens, with extension to 1M using supported scaling. Test
native context first; advertising 1M is not proof of allocation or useful recall.

| Experiment     | Per-conversation total context | Simultaneous sequences | Purpose                                   |
| -------------- | -----------------------------: | ---------------------: | ----------------------------------------- |
| Reference      |                            32K |                      1 | Repeat the existing filled-context result |
| Working agent  |                           128K |                      1 | Establish useful single-agent capacity    |
| Native ceiling |                           256K |                      1 | Test capacity and retained task facts     |

At each size fill most of the window, reserve output, and test retrieval at multiple
depths plus a multi-turn edit depending on earlier facts. Report advertised,
allocated, and effective context separately. Measure fresh and cached prefill,
per-request decode, aggregate throughput, peak memory and completed tasks/hour.
Verify actual `n_ctx_slot` and overflow behavior from logs with `--parallel 1`.
Account for KV, recurrent state, checkpoints and prompt-cache RAM. Multiple-context
capacity remains an open question, but concurrent generation is outside the current
run plan. Allocation arithmetic alone would not prove useful simultaneous capacity.

Only after native 256K is useful, try a bounded 512K/1M extension if the runtime
supports this architecture's scaling correctly. Longer context can increase prefill
and displace resident weights, so it is a capacity/quality experiment, not a free fix.

### 2. Compare published capability with neighboring GPT/Claude effort levels

Keep a compact dated comparison of the aggregate index and its individual coding,
agentic, long-context and grounding measures where available. Use the same benchmark
version and explicit effort level; preserve served-model/provider and precision
caveats. The [September 26 neighbor snapshot](runs/2026-09-26_harbor_review/aa_neighbours.json)
extends the table above to Sonnet/Opus and adjacent GPT effort levels. Qwen is close
to Sol medium on the aggregate index and close to Sol high on this Terminal-Bench
score; neither means interchangeability across tasks. The [per-evaluation breakdown](runs/2026-09-26_harbor_review/README.md#trust-and-per-evaluation-differences)
shows substantial reversals across metrics. Neither aggregate positioning nor
provider scores establish the quality retained by the local quantization.

### 3. Trade precision and reasoning against context and latency

Vary one axis at a time. 'Lower quant' can mean either fewer bits (smaller/faster)
or less quantization (more precision); these address different hypotheses.
[Current GGUF sizes](https://unsloth.ai/docs/models/qwen3.8-next): Q4_K_XL 111.3 GB,
IQ4_XS 93.7 GB, Q3_K_XL 90.0 GB, Q5_K_XL 158.3 GB. These are file sizes,
not required VRAM, and the large embedding tables complicate simple bits/parameter
intuition.

- Keep Q4 as control and try Q5 first to test quality retention, once safe placement
  is established. Its larger working set may cost speed or page-cache capacity;
  more bits do not guarantee a better completed task within the available budget.
- IQ4_XS is the subsequent smaller candidate if reducing CPU/offload traffic could
  free context or improve latency. Assess it on the same tasks; do not jump directly
  to a very low-bit checkpoint.
- Set reasoning explicitly. The official default is xhigh; our initial probes used
  medium. Compare medium and xhigh under an adequate, explicit output allowance.
  Short-output protocol probes cannot establish the model's reasoning ceiling.
- Try Q8 versus smaller KV formats only with supported kernels and recall checks.
  Test MTP/speculation last, measuring accepted draft tokens and end-to-end gains;
  faster token loops that increase SSD reads need not improve agent completion.

### 4. Bounded local capability validation

Terminal-Bench 4.0 is a substantial agent evaluation, worth a carefully controlled
rerun. Reserve 24–48 hours for a serial Q4/Q5 comparison on a predetermined CPU-only
subset, preserving original task verifiers and deadlines. This is a compute budget,
not a promise to finish every selected task. Record unfinished work explicitly.

The [next-run protocol](runs/2026-09-26_harbor_review/NEXT_RUN.md) specifies preflight,
matched settings, scheduling and reporting. Check the installed agent's context
policy before spending the budget. Keep an uncompacted Mini-SWE track distinct from
practical OpenCode compaction; a larger window alone cannot bound a growing transcript.
Do not launch another full 66-task suite until the smaller run establishes that the
configuration is usable and the result would answer a remaining question.

Keep task/scoring implementation in `x/local_llm/eval`, with records here; Agentplane
is optional. The two tasks in #7910 are smoke gates, not evidence of hard-task quality.
A selected CPU-only subset cannot establish equivalence to AA's full-suite score.
Context failures still count as failures of the local configuration; report their
cause rather than removing them from the denominator. A matched cloud control would
be more informative than comparing headline percentages, but requires an available,
authorized API budget; no paid comparison is scheduled here.

### 5. Deployment follow-up after the interesting measurements

Making Ollama serve this efficiently, choosing a persistent deployment and wiring
routes are useful operational work, with lower priority than context/concurrency
and capability comparison. Keep the already working SSD llama.cpp path while
answering those questions. Change runtime only when correctness or measured capacity
requires it; do not make a broad runtime bake-off the opening experiment.

When deploying, compare current Ollama and llama.cpp on SSD using matched model,
context, template and placement. Measure cold loading and conversation reuse,
retaining desktop headroom. Use server timings and existing `pidstat`/`iostat`/cgroup
metrics if explaining a bottleneck becomes necessary. Promote one useful configuration
through GitOps and its intended client after selection; Agentplane stays optional.

## Explore alternative models and runtimes

Prioritize **GLM-5.3-Flash** for the next conventional offload feasibility check:
its [published AA index](https://artificialanalysis.ai/models/glm-5-3-flash) is about
42 versus Qwen's 40, and its model card advertises 1M context. Neither establishes
local IQ3 quality or fit, so first verify current architecture/runtime support,
placement, actual context, and a useful task. The larger 18B active working set may
cost substantial latency. Refresh **DeepSeek-V4-Flash-0731** as an alternative using
the July runtime knowledge, without assuming its older local results predict the
new checkpoint. Other candidates earn attention through better external capability,
more usable context/concurrency, or a materially different memory/compute approach. Keep the DeepSeek-V4.1 SSD
streaming feasibility lane from the source-linked shortlist: inspect
the runtime first, archive unused checkpoints with verification before downloading
its ~502 GB weights, and use a cache sized for actual available host RAM.
One successful hard task can justify a slow configuration; a token-rate result alone
cannot. Do not postpone this lane for exhaustive Qwen tuning. RAM reallocation,
host changes or full NixOS activation remain separate decisions requiring approval.

## Uncertainty register and competing outcomes

| Question                                                 | Current state                                                          | Discriminating action                           |
| -------------------------------------------------------- | ---------------------------------------------------------------------- | ----------------------------------------------- |
| Does GLM IQ3 retain an advantage over Qwen Q4/dense FP8? | Unknown; model cards cannot settle quantized local quality             | Matched coding tasks                            |
| Does V4.1 remain useful with a smaller host cache?       | Plausible external evidence, unmeasured here                           | Cache sweep under current RAM allocation        |
| Is GPU instability resolved?                             | Both responsive, no errors in queried log; sustained stability unknown | Bounded load plus kernel monitoring             |
| Can 128K context support productive multi-turn work?     | OpenCode success across compaction; uncompacted Mini-SWE hit 128K      | Filled-context tool trajectories                |
| Will changing expert demand erase warm-cache speed?      | Known risk for streaming                                               | Diverse sequential tasks, not repeated prompts  |
| Which runtime best uses two non-P2P GPUs?                | Dense tensor split helps decode; Flash concurrency unmeasured          | Same checkpoint, matched placement and workload |

Possible outcomes: large offloaded model wins on difficult tasks; resident dense model beats degraded large quants;
new streaming implementation raises the ceiling; integration failures dominate; no candidate is reliable without frequent rescue.
No numerical probabilities or future speed estimates are justified by this review.

## Decision branches and stopping criteria

- If a model cannot pass encoding/tool fixtures, resolve that before expensive agent runs.
- If placement fits but task quality degrades, compare a higher quant or a smaller higher-precision model.
- If V4.1 works but faults excessively, distinguish RAM-cache scarcity, disk latency, and implementation overhead before
  considering RAM/storage changes. Do not repeat the unsafe historical 112 GiB VM allocation at the host's expense.
- Retain slow configurations that solve additional tasks without disproportionate human intervention.
- Stop a tuning branch when changes do not improve task quality, usable context, or completion time.
- Completion means one reviewed local configuration finishes representative coding jobs through the intended agent client,
  plus a measured frontier and documented failures. Loading a checkpoint or passing a weather call is insufficient.

## Planning assumptions

Candidate ordering and the bounded subset size are planning choices. Freeze task
selection before observing new outcomes; expand only under a declared budget.
No promised tokens/s for new models; no claimed IQ2/IQ3 capability retention; no hardware-purchase recommendation yet.
Illustrative decode-only arithmetic: 10,000 output tokens take ~56 minutes at 3 tok/s or ~17 minutes at 10 tok/s,
before prefill, tools, retries, and additional turns. Actual agent jobs can generate far more tokens.

## Experiment records and deployment

Keep one dated `runs/<run-id>/` directory per experiment, with its exact launch configuration,
measurements, anomalies, and verdict. Pin the runtime commit or image digest and checkpoint revision.
Add a source-linked row to <results.md>; preserve accepted historical measurements. Use `local` for
measured results, `local~` for smoke probes, `ext` for comparable external evidence, and `ext?` when
precision, runtime, or task protocol differs. Avoid a new evaluation framework or generated report pipeline.

Use ad-hoc Kubernetes workloads where the runtime fits that environment; use host runs for experiments
requiring direct storage/cache control. Promote only a selected configuration to Flux and LiteLLM, then
verify tool calls and a representative coding task through the intended agent client. This PR records
research, experiment recipes and captured responses. It changes no deployed workload
or storage allocation; production routing is a separate decision.
