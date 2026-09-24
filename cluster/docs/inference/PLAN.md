# Wyrm2 capability-first inference plan (WIP)

Updated: 2026-09-24. Research and read-only inspection; no workloads changed or weights downloaded.

## Objective and decision

User priority, verbatim: "The strongest model this machine can run, even if slow".
Also refresh releases since the last substantive inference experiments, July 17–18.
September Git edits mostly maintain paths and deployment wiring; they are not September model evaluations.

Choose a model, quantization, runtime, reasoning setting, and usable context together.
Optimize task success and independence from human rescue first; retain latency measurements to expose the cost.
No blanket tokens/s cutoff. A slow model earns its place by solving tasks the faster ones cannot.

## Current machine: directly observed

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
no new model performance has been measured in this refresh.

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
Expand the existing VM model disk to accommodate the 502 GB checkpoint and working headroom.
The current 500 GiB virtual allocation is not a physical capacity ceiling; see the subsequent storage inspection below.
Keep every expert; caching changes placement, whereas dropping experts changes the model.

## Storage recommendation

Subsequent September 24 inspection of Atlas confirmed that `virtio8` is
`local-zfs:vm-110-disk-7`, backed by the 4 TB Sabrent NVMe in `rpool`.
ZFS reported about 1.03 TB (956 GiB) available to datasets, shared with other VMs;
the pool had about 1.22 TB physically free. These are different accounting figures,
not additive capacity. The HDD share had about 27 TiB free.
Root free space had increased to 105 GiB by this later check; the earlier disk-pressure
observation above was not rechecked at that time.

| Storage                             | Current allocation | Proposed allocation | Reason                                                          |
| ----------------------------------- | ------------------ | ------------------- | --------------------------------------------------------------- |
| SSD models (`virtio8`)              | 500 GiB            | 1,024 GiB           | V4.1 plus comparison checkpoints, with inactive models archived |
| Bazel output/cache (`virtio4`)      | 150 GiB            | 250 GiB             | Only 4 GiB free at inspection                                   |
| Root (`scsi0`)                      | 500 GiB            | 500 GiB             | 105 GiB free after cleanup                                      |
| Games, containerd, Kubernetes disks | Existing           | Existing            | No expansion needed for this program                            |

Rename the model mount from `/var/lib/colibri` to **`/var/lib/llm-models`**,
with the human-facing description **SSD model storage**. Keep the existing virtual
disk and ext4 filesystem. The generic name describes all inference runtimes;
Colibri remains the name of that runtime's checkout and historical experiments.
Update the NixOS mount and tmpfiles ownership, Terraform description, monitoring
mountpoint selector and generated manifest, and executable launcher defaults together.
Stop consumers before changing the mount and verify the new path is mounted before
resuming downloads or inference. Historical result narratives should retain the paths
actually used; runnable recipes should use the new path or an explicit override.

The operator is comfortable archiving or deleting unused models. Candidate archives
are GLM-5.2 Colibri (358 GiB) and the July DeepSeek IQ2 checkpoint (86 GiB).
Confirm they are unused, copy to HDD and verify before removing the SSD copies;
retain small run records. With both archived, the 502 GB V4.1 checkpoint and three
comparison checkpoints total roughly 770 GiB, fitting the proposed model filesystem.
Use 400 GiB of pool-available SSD space as an operating headroom target, checking it
before downloads. Existing disks are thin-provisioned: shrinking an empty allocation
does not recover the nominal size as physical space.

## Ranked execution plan

1. **Prepare capacity for experiments.** Recheck root disk pressure after the operator's garbage collection;
   if it persists, inspect kubelet eviction signals and remaining consumers before more cleanup. Account for
   Ollama's two-GPU reservation before trials. Apply the storage recommendation above, refreshing shared pool
   capacity first. `virtio8` is declared in <../../terraform/main/proxmox-vms.tf>; its ext4 mount in
   <../../../nix/nixos/hosts/wyrm2/default.nix> already has `autoResize = true`.
   Review the Terraform plan for in-place disk growth; verify guest block-device and filesystem sizes
   after applying through the normal bootstrap path. Enlarging this disk does not enlarge the separate root disk
   or Ollama PVC. No resize, mount rename, or model archival is included in this documentation PR.
   Run a bounded GPU workload with failure monitoring; current idle health is insufficient proof.
2. **Repair the small measurement harness.** Tokenize exact inputs, reserve output, record termination reason,
   complete reasoning/content/tool output, server timings and wall time. Separate genuinely new prompts, cached prefixes,
   and exact repeated generations. Preserve historical runs; write new dated results.
3. **Probe the strongest plausible candidates.** First GLM-5.3-Flash IQ3, Qwen3.8-Flash-Next Q4, and DeepSeek 0731.
   Start at 8K to establish correct inference/tool use, then filled 32K and 128K. Sweep GPU expert placement,
   keeping asymmetric desktop headroom; measure RSS, VRAM, disk reads and page faults. Qwen3.8-27B is the resident control.
   A failing parser gets a bounded investigation before a model is rejected.
4. **Pursue V4.1 feasibility without waiting for every comparison.** Inspect/build the matching fork and validate its
   fixtures first. Use the expanded model disk after the recipe passes inspection. Start on GPU1 with a conservative
   host cache, then test two-GPU placement if supported. Compare direct/buffered I/O with real changing prompts.
   Do not copy the author's 72 GiB pinned cache into a workstation with only ~70 GiB currently available.
5. **Measure coding capability on one shared scaffold.** Start with roughly 10–20 matched tasks as an operational
   screening budget (not a powered benchmark): bounded fixes, multi-file changes, debugging, test construction, review.
   Use existing evaluators/Props where appropriate. Score actual held-out tests and review correctness, not self-reports.
   Give slow models enough wall time; separately report equal-output-budget and overnight-budget results.
   Track success, truncations, tool/parser failures, loops, human rescues, total reasoning/output, task wall time,
   and energy if available. Expand only close comparisons; don't infer tiny percentage differences from this sample.
6. **Tune the winner.** Increase precision before sacrificing quality for speed. Compare reasoning settings and context.
   Try MTP/speculation only after a non-speculative baseline: Colibri already showed that acceptance can look good
   while extra expert traffic erases the speed benefit. Promote a kept configuration through the actual gateway/agent path.

Concrete useful deployments to evaluate: overnight issue-to-patch worker, independent review/debugging second opinion,
and long-context repository analysis. Capability remains the selection criterion; these tasks make long waits tolerable.

## Uncertainty register and competing outcomes

| Question                                                 | Current state                                                          | Discriminating action                           |
| -------------------------------------------------------- | ---------------------------------------------------------------------- | ----------------------------------------------- |
| Does GLM IQ3 retain an advantage over Qwen Q4/dense FP8? | Unknown; model cards cannot settle quantized local quality             | Matched coding tasks                            |
| Does V4.1 remain useful with a smaller host cache?       | Plausible external evidence, unmeasured here                           | Cache sweep under current RAM allocation        |
| Is GPU instability resolved?                             | Both responsive, no errors in queried log; sustained stability unknown | Bounded load plus kernel monitoring             |
| Can 128K context support productive multi-turn work?     | Historical needle results only for some models                         | Filled-context tool trajectories                |
| Will changing expert demand erase warm-cache speed?      | Known risk for streaming                                               | Diverse sequential tasks, not repeated prompts  |
| Which runtime best uses two non-P2P GPUs?                | Unknown for new candidates                                             | Same checkpoint, matched placement and workload |

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

Candidate ordering and the 10–20-task screen are judgment calls, to be revised after first results.
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
research and planned experiments only; it changes no deployment, storage allocation, or benchmark code.
