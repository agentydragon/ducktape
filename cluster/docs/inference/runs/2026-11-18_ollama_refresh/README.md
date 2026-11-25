# November 2026 Flash-Next serving refresh

November 18, 2026, wyrm2. Flash-Next remains the main local model. Compare the
existing IQ4_XS on a newer runtime with the self-calibrated 2.68-bit EXL3 build.
For a smaller GGUF, the first candidate is Unsloth Dynamic v3.1 UD-IQ3_XXS.

## Live Ollama baseline

Ollama 0.34.4 on two RTX 5090s, with Q8 KV and one slot. The GPUs are connected
through the PCIe host bridge, and NVIDIA reports peer reads unsupported. The
model was initially unloaded. For each alias, I replayed the
[September request](../2026-09-27_ollama_ssd/perf.request.json) twice, changing
only the model alias and idle retention (2 minutes). Each request processed a
51-token prompt and generated 1,024 tokens with thinking disabled. Timings
are Ollama's counters. Each cell represents one request.

| Configured context | Request     | Load (s) | Prompt eval (s) | Decode (s) | Decode (tok/s) | Total (s) |
| ------------------ | ----------- | -------: | --------------: | ---------: | -------------: | --------: |
| 256K               | First       |  168.249 |           6.748 |     34.602 |          29.59 |   209.604 |
| 256K               | Warm repeat |    0.001 |           0.172 |     25.300 |          40.47 |    25.563 |
| 128K               | First       |   99.684 |           4.866 |     28.716 |          35.66 |   133.271 |
| 128K               | Warm repeat |    0.002 |           0.152 |     21.329 |          48.01 |    21.577 |

The [September run](../2026-09-27_ollama_ssd/README.md) is the local filled-context
reference. It spent 191 seconds prefilling 145K tokens, recalled the marker,
and decoded a cached continuation at 29.07 tok/s. Full-window input and
concurrent serving still need local validation.

## Serving candidates

1. **ExLlamaV3 + TabbyAPI, self-calibrated 2.68-bit EXL3.** A November run on
   two 5090s reported 278 tok/s single-stream decode and 227 output tok/s
   including request overhead on an agent replay subset. A separate 120K
   prompt reached its first token in 9.6 seconds with a warm model and cold
   prefix. The quant author reported SWE-bench mini agentic at 76% for both
   the 2.68-bit and original checkpoints, and ERQA at 65.5% vs 68.8%. These
   quality figures are single runs. An independent dual-5090 test with an
   earlier 2.50-bit setup completed a full-window request, but a short
   request took 226 seconds to start during a wave of long requests.
   Start with one active session. [November serving run](https://github.com/adrienbrault/qwen3.8-flash-next-2x-rtx5090) ·
   [Quant evaluation](https://huggingface.co/turboderp/Qwen3.8-Flash-Next-exl3) ·
   [Independent load test](https://scalably.io/blog/qwen-flash-next-dual-rtx-5090-serving-benchmark)
2. **Current llama.cpp with the existing IQ4_XS.** Keep the Q8 KV and target
   weights for the first comparison. Measure native multi-token prediction
   (MTP) off and on. The upstream prototype improved coding decode 1.48× on
   IQ4_XS on DGX Spark. Test the GPU expert cache separately if host-offloaded
   experts limit decode. Its upstream test went from 30.8 to 54.5 tok/s on
   one 5090 with Flash-Next Q4_0 at 32K. Profile draft acceptance, cache hits
   and speed at long context before trying a second drafter such as DFlash2.
   [MTP test](https://github.com/ggml-org/llama.cpp/pull/29761) ·
   [Expert cache test](https://github.com/ggml-org/llama.cpp/pull/29887) ·
   [Draft sidecars](https://huggingface.co/ggml-org/Qwen3.8-Flash-Next-GGUF)
3. **Unsloth Dynamic v3.1 UD-IQ3_XXS.** The download is 83.6 GB versus 93.7 GB
   for the deployed August IQ4_XS. Unsloth reports 55.1 vs 55.8 BF16 on
   DeepSWE 1.1, 70.8 vs 68.3 on Terminal-Bench 2.1, and 90.5 vs 90.4 on
   LiveCodeBench v6, with similar reasoning length. The quantizer ran both
   sides of that comparison. Its Terminal-Bench protocol used 82 of 89 tasks.
   The `UD-IQ4_XS` label was also refreshed on November 4. Ducktape's pinned
   second shard hash matches the August revision `2c41bd2`. The November
   `814db05` revision has four shards and separates the n-gram table. Use an
   exact model revision for any comparison. New llama.cpp builds can swap the
   n-gram table with `--ple`, which lets us test table precision independently.
   [Benchmark chart and n-gram options](https://unsloth.ai/docs/models/qwen3.8-next) ·
   [Benchmark methodology](https://unsloth.ai/docs/basics/dynamic-3.0-ggufs/dynamic-3.1-ggufs) ·
   [IQ4_XS history](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF/commits/main/UD-IQ4_XS)
4. **NVFP4 + Basalt.** NVIDIA's Flash-Next checkpoint scored 82.9 vs 83.3 FP8
   on Terminal-Bench 2.1 and 74.1 vs 71.9 on AA long-context recall. Basalt's
   5090 + 5060 Ti tests show faster decode and prefill with native FP4
   kernels, expert caching, and MTP. The Basalt pack changes dense tensor
   precision, so NVIDIA's results describe the source checkpoint. Qualify
   memory use and long prefill on wyrm2's PCIe topology.
   [NVIDIA evaluation](https://huggingface.co/nvidia/Qwen3.8-Flash-Next-NVFP4) ·
   [Basalt speed and precision](https://github.com/jesdga95/basalt/blob/main/docs/precision/qwen38-flash-next.md)

## Other models

- **Occamy-1.0 NVFP4** is a one-GPU candidate for routine tool work at 23.9 GB
  on disk. Accio reports 59.0 on Terminal-Bench 2.1 for the original Occamy
  and 49.5 for its Qwen3.6-35B parent. The published NVFP4 checks cover basic
  runtime behavior. Use the existing [Qwen3-Coder-30B-A3B AWQ run](../2026-07-17_e1_qwen3coder_awq/README.md)
  as the local fast control at 199 tok/s at 128K with single, parallel and
  multi-turn tool calls passing. [Occamy evaluation](https://huggingface.co/Accio-Lab/occamy-1.0) ·
  [NVFP4 checkpoint](https://huggingface.co/Accio-Lab/occamy-1.0-NVFP4)
- **Ripple-65B Preview** is a newer coding challenger with 80.5 on
  Terminal-Bench 2.1 and 44.3 on DeepSWE 1.1 in the publisher's BF16 evaluation.
  The conservative NVFP4/BF16 checkpoint is 57.2 GB. Its dense weights and
  remaining KV budget make it a quality comparison with a tight memory budget
  at long context. [Ripple evaluation](https://huggingface.co/ricursiveAI/Ripple-65B-Preview) ·
  [Mixed-precision files](https://huggingface.co/ricursiveAI/Ripple-65B-Preview-NVFP4/tree/main)

## SSD placement

Live readings on November 18. Filesystem availability is from `df -kPT`.
Thin-pool usage is from `lvs` and ZFS availability is from `zfs list -p`.

| Location                             | Capacity and available space                                         | Backing                                                        |
| ------------------------------------ | -------------------------------------------------------------------- | -------------------------------------------------------------- |
| `ollama/qwen38-iq4` at `/ssd-models` | 100 GiB claim, **5.6 GiB available**                                 | Strict-local `piraeus-ssd-local`, read-only in the serving pod |
| `ollama/models` at `/models`         | 200 GiB claim, 78.2 GiB available                                    | `piraeus-hdd-local`, writable Ollama registry                  |
| Wyrm2 `piraeus_ssd/data`             | 142 GiB thin-data pool, 65.05% used, **49.6 GiB physical data free** | 150 GiB NVMe-backed Proxmox zvol                               |
| Wyrm2 `/games`                       | 500 GiB disk, **317.4 GiB filesystem space available**               | Live VM 110 `virtio1`, Proxmox `local-zfs` on Atlas NVMe       |
| Atlas `rpool/data`                   | **1.31 TiB available**                                               | Parent SSD dataset shared by the two wyrm2 disks above         |

Use `/games/llm-models/` for a host-side pilot on wyrm2. Atlas reports that
`rpool` is backed by one Sabrent Rocket 5 NVMe. Put both the August IQ4_XS
shards and the trial EXL3 files on this same filesystem for the comparison.
They total about 179 GiB before draft artifacts. The
[pinned 2.68-bit EXL3 release](https://huggingface.co/turboderp/Qwen3.8-Flash-Next-exl3/tree/2.68bpw_h6_ng8)
takes 91.6 GiB, including a 51.8 GB n-gram table. Its runtime can read the
table during generation, so storage matters beyond model load. Wyrm2's
`/tmp` is HDD-backed and cleaned by tmpfiles.

For a persistent cluster route, give the candidate its own 128 GiB
`piraeus-ssd-local` PVC on wyrm2. On November 21, wyrm2's Piraeus SSD backing
disk was grown from 150 to 600 GiB and its thin-data pool to 592 GiB. The claim
size covers one EXL3 package plus draft artifacts, filesystem overhead, and
download staging. Download directly
into the claim and mount weights read-only in the serving pod. Before creating
the claim, verify that the extra space is visible at each layer (Atlas zvol,
guest block device and LVM PV, thin-data LV, and LINSTOR's free-space report).
Recheck pool utilization after populating it. See [Piraeus capacity policy](../../../piraeus.md#capacity-policy)
and the [Ollama mount layout](../../../../cdk8s/ollama/README.md#ssd-weights-alongside-hdd-models).

## Local comparison

Keep the live Ollama result as the deployment reference. For the matched
storage run, compare the August GGUF on current llama.cpp with and without MTP
and EXL3 with native verification and one active session. Use fixed coding
transcripts with 32K and 128K cold prompts, a cached tool-result continuation,
and a near-240K prompt with output headroom. Report model-load time, cold-prefix
prefill, and warm decode separately, recording whether model files were already
cached by the host. Record cached input tokens, reasoning tokens, peak VRAM/RAM,
and wall time to a passing result. Verify the LiteLLM tool-call
round trip, structured output, and mid-conversation system reminders with the
actual clients. Then probe a short request arriving during one long prefill.
Use published quality evaluations for the full suites.
