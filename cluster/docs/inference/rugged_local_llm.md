# Rugged local LLMs

What runs on `rugged` (Core Ultra 7 268V, 30 GiB RAM shared with the iGPU, Arc 130V/140V
iGPU, Lunar Lake NPU) with no network or cluster dependency, and how to run it. The earlier
Ollama speed table contains single-run sanity checks; repeated measurements below identify
their sample counts and limitations. Host config lives in `nix/nixos/hosts/rugged/` and
`nix/home/hosts/rugged-opencode.nix`; hardware debugging in `nix/debug/rugged/hw/`.

## Backends

| Backend                                   | State (2026-10-13)                                      | Gemma 4 E2B decode | Notes                                                                                                         |
| ----------------------------------------- | ------------------------------------------------------- | -----------------: | ------------------------------------------------------------------------------------------------------------- |
| Ollama 0.40.2, Vulkan on the iGPU         | Running on `127.0.0.1:11436`, model `gemma4:e2b-it-qat` |        12-17 tok/s | Cold load 31 s. Earlier notes measured ~22 tok/s.                                                             |
| LiteRT-LM CLI 0.17.0, Vulkan on the iGPU  | Installed, no server running                            |           26 tok/s | 512-token prefill at 648 tok/s. Earlier notes measured 38-40 tok/s with speculative decoding (not re-tested). |
| LiteRT-LM CLI, CPU                        | Works                                                   |           15 tok/s | Prefill 256 tok/s.                                                                                            |
| OpenVINO GenAI 2026.4.1 on the NPU        | Temporary diagnostic environment, generation verified   |                  - | TinyLlama and Qwen Coder execute; see [NPU](#npu).                                                            |
| llama.cpp OpenVINO on the NPU             | Disabled (`ducktape.localLlm.npu.enable = false`)       |                  - | This service runtime remains unverified.                                                                      |
| llama.cpp Vulkan or SYCL, OpenVINO on GPU | Untested                                                |                  - | IPEX-LLM is dead (archived upstream, Ollama frozen at 0.9.3).                                                 |

## Running the models

### Ollama (always on)

The NixOS service (`local_llm_arc.nix`) is already running with `OLLAMA_IGPU_ENABLE=1` and
`OLLAMA_CONTEXT_LENGTH=131072`. It binds loopback only.

```bash
curl -s http://127.0.0.1:11436/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"gemma4:e2b-it-qat","messages":[{"role":"user","content":"hi"}]}'
journalctl -u ollama | grep -E 'prompt eval time|eval time'   # server-side tok/s
```

### LiteRT-LM

The CLI (`.#litert-lm`) has model `gemma4-e2b-it` imported. **Gotcha:** the GPU backend fails
in a plain shell because `libvulkan.so.1` is not on the library path. Point it at a 64-bit
`vulkan-loader` (a 32-bit one fails with `ELFCLASS32`) and the Intel ICD:

```bash
export LD_LIBRARY_PATH=<64-bit vulkan-loader store path>/lib
export VK_ICD_FILENAMES=/run/opengl-driver/share/vulkan/icd.d/intel_icd.x86_64.json
```

The OpenAI-compatible server was exercised on loopback with
`litert-lm serve --host 127.0.0.1 --port 9379 --enable-speculative-decoding=true`.
Three repeated requests returned the expected `read_file(path="main.py")` tool call;
a tool-result follow-up correctly answered `42` for `print(6 * 7)`. These are protocol
probes, not a completed coding-agent task. Requests used model
`gemma4-e2b-it,gpu,32000`, `reasoning_effort: "none"`, temperature zero, and
`max_completion_tokens: 64`. The installed handler reads `max_completion_tokens`,
not `max_tokens`. Exact [requests and responses](runs/2026-10-13_rugged_litert/)
are retained.

[PR #10442](https://github.com/agentydragon/ducktape/pull/10442) adds the dynamically
loaded Vulkan library to the Nix CLI wrapper. Its built executable ran on the GPU
with `LD_LIBRARY_PATH` unset. The built-in benchmark, one warmup followed by three
512-input/128-output runs at 32k context with speculative decoding, reported mean
prefill **151.71 tokens/s**, decode **29.58 tokens/s**, TTFT **3.449 s**, and
initialization **10.913 s**. The CLI reports means without per-run dispersion.
The model file SHA-256 is
`181938105e0eefd105961417e8da75903eacda102c4fce9ce90f50b97139a63c`.

A 2048-input run was interrupted during warmup after 231 s to release the device
for other experiments; it supplies no throughput result. Nix/build preparation
overlapped parts of these initial probes. Do not use that interruption to infer a
model limit or compare the short-prompt mean as a controlled backend benchmark.

### Offline coding harness

The dedicated `rugged-opencode-offline` Home Manager command in
[PR #10458](https://github.com/agentydragon/ducktape/pull/10458) uses a fresh home
directory, a minimal OpenCode configuration, and only local file and shell tools.
It runs in a network namespace with a Unix-socket bridge to the loopback Ollama
endpoint. The repository remains writable; Git metadata is read-only. With the
model already cached, this supports editing and testing without external network
access. Ordinary OpenCode profiles keep their existing configuration.

The tool-discovery failure came from combining deprecated `agent.tools` settings
with permissions: the resulting trailing wildcard denial suppressed all tools.
The dedicated profile uses permissions alone, with a wildcard denial followed by
explicit local-tool grants. Wire inspection of the built launcher confirmed six
tools, temperature 0.2, top-p 0.95, and reasoning disabled.

For a stronger offline check, a temporary Ollama server and the built launcher
both ran inside an outer network namespace. The backend used Vulkan with all
37 Qwen3 4B layers offloaded; a public TCP connection failed with network
unreachable. The real JSONL-transform task from the frozen PR #7910 fixture ran
for about 77 seconds and exercised reads, edits, and shell tests. Its final
independent verifier passed only **2 of 4 tests**: literal Unicode output and
rejecting a string-valued `tags` field were wrong. The model claimed success,
but that claim is not counted as a successful task. The initial failure and any
verifier-guided repair are recorded separately from HumanEval+ scores.

### Earlier OpenCode readiness check

Tool calling works against Ollama and Gemma 4 E2B QAT: 3 of 3 raw `/v1/chat/completions`
probes with one tool returned a well-formed `tool_calls` entry, and OpenCode 1.19.0 completed
a read, edit, run task (fix `sum(xs) / (len(xs) + 1)` in a small script, run it) in 6 model
turns, about 97 s. Run it from a scratch environment with a config that sets `tool_call: true`
on the `rugged` provider (`baseURL` `http://127.0.0.1:11436/v1`, model `gemma4:e2b-it-qat`):

```bash
S=$(mktemp -d); mkdir -p $S/home $S/xdg $S/work
# $S/oc.json: provider "rugged" as above, tool_call=true, permission edit/bash=allow, mcp disabled
cd $S/work && env -i PATH="$PATH" HOME=$S/home XDG_DATA_HOME=$S/xdg OPENCODE_CONFIG=$S/oc.json \
  opencode run --pure --format json -m rugged/gemma4:e2b-it-qat "<task>"
```

`--format json` emits one event per step with timestamps and token counts.

**Gotcha: keep `HOME` empty.** With the real `$HOME`, OpenCode loads 61 skills and the first
prompt is about 16k tokens. Ollama reprocesses the whole prompt for this model (no cache
reuse for sliding-window-attention models), so prefill alone takes about 50 s at 500 falling
to 300 tok/s, and the one such run produced no output in 10 minutes (not root-caused). With an
empty `HOME` the first prompt is 6.9k tokens and follow-up turns reuse the cache.

## Other models

Speed and memory on the iGPU at `num_ctx=32768` (single runs; method, prefill table and caveats in
<runs/2026-10-13_rugged_model_speed/README.md>):

| Model                           | `size_vram` | Decode at 0.5k prompt | Decode at 21k prompt |
| ------------------------------- | ----------: | --------------------: | -------------------: |
| `qwen3:4b-instruct-2507-q4_K_M` |     7.0 GiB |            32.1 tok/s |           12.3 tok/s |
| `gemma4:e4b`                    |    unclear¹ |            31.1 tok/s |           17.7 tok/s |
| `qwen2.5-coder:7b`              |     6.1 GiB |            21.0 tok/s |           14.7 tok/s |
| `qwen3:8b`                      |     9.2 GiB |            18.9 tok/s |            9.8 tok/s |

¹ Ollama reports 0.27 GiB for `gemma4:e4b`, which does not match its weights.

- No crash at prompts up to ~21k tokens with `num_ctx=32768` on any of the four. Above 32k and
  the service's 131072 default remain untested.
- Prefill is the cost: 400-600 tok/s at 0.5k falls to 65-90 tok/s at 21k, so a 21k first prompt
  takes 4-6 minutes. Follow-up turns reuse the cache.
- These earlier runs measured speed only; the repeated coding campaign below is separate.

### Context capacity

Distinguish the model's advertised limit, the server's allocation, and an actual
completed prompt. The four Ollama candidates above completed roughly 21k-token
prompts with 32,768 total context allocated. The live service's 131,072 setting
does not establish that it processed a full 128k prompt. Input, chat formatting,
tool definitions, history, and generated output share the context budget.

Qwen3-4B-Instruct-2507 declares
[262,144 positions](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507/blob/main/config.json).
Its observed 32k F16 KV allocation is 4608 MiB. Scaling to 128k gives 18 GiB
for F16 KV alone; Ollama's documented
[q8_0 cache reduction](https://docs.ollama.com/faq) suggests roughly 9–10 GiB
with quantization, before weights and other buffers. This is a capacity estimate,
not a successful 128k execution or a quality result. A staged experiment must
check actual cache type, accelerator placement, input counts, truncation, memory,
prefill time, and retrieval before recommending it for agent use.

The tested OpenVINO Qwen pipeline reserves 8192 input tokens plus 2048 response
tokens; its longest completed diagnostic prompt so far was 4538 tokens.

### Repeated speed probes

The [battery run](runs/2026-10-13_rugged_repeated_speed/) uses three varied-prefix
prompts at each size, 32,768 total context, and 256 generated tokens. The short
prompts contain roughly 474–513 tokens; long prompts roughly 3565–3738, depending
on tokenizer. Values below are medians; parentheses give observed decode ranges.
Native Ollama counters supply prefill and decode rates. Total request duration
includes both and is not time to first token.

| Model             | Short prefill tok/s | Short decode tok/s | Long prefill tok/s | Long decode tok/s | Long request seconds |
| ----------------- | ------------------: | -----------------: | -----------------: | ----------------: | -------------------: |
| Qwen3 4B Instruct |               514.3 |   17.1 (15.9–22.1) |              215.5 |   10.5 (7.6–10.8) |                 47.3 |
| Gemma 4 E4B       |               319.0 |   27.4 (20.2–28.2) |              139.8 |  25.1 (18.2–25.9) |                 36.0 |
| Qwen2.5-Coder 7B  |               354.0 |   14.4 (11.6–14.9) |              254.0 |  13.7 (10.1–14.2) |                 34.5 |
| Qwen3 8B          |               114.4 |      5.3 (5.2–5.9) |               79.1 |     4.6 (4.1–6.0) |                108.2 |

Gemma 4 E2B QAT completed only one short battery sample before the phase ended
(12.0 decode tokens/s); it is excluded from the repeated table. The explicitly
contended initial Qwen batch is also excluded. The raw summary includes all
completed samples and their denominators.

The [AC rerun](runs/2026-10-14_rugged_ac_speed/) completed all five models with
the same protocol, three repetitions per cell:

| Model             | Short prefill tok/s | Short decode tok/s | Long prefill tok/s | Long decode tok/s | Long request seconds |
| ----------------- | ------------------: | -----------------: | -----------------: | ----------------: | -------------------: |
| Qwen3 4B Instruct |               612.6 |    11.9 (9.4–14.1) |              289.6 |     8.4 (8.1–9.8) |                 50.7 |
| Gemma 4 E4B       |               305.6 |   14.0 (11.1–15.7) |              137.8 |  13.4 (12.5–14.2) |                 47.4 |
| Qwen2.5-Coder 7B  |               339.1 |      5.4 (5.2–5.4) |              279.4 |     4.9 (4.7–5.2) |                 70.1 |
| Qwen3 8B          |               252.0 |      4.7 (4.3–4.8) |              248.3 |   12.2 (5.3–12.6) |                 38.7 |
| Gemma 4 E2B QAT   |               581.5 |   34.0 (33.2–34.4) |              293.9 |  27.5 (25.4–28.0) |                 22.2 |

AC is not a controlled treatment here: workload conditions changed over the
serial sweep. Monitoring captured swap-in around 20 MB/s, substantial I/O wait,
and a 95°C CPU-package snapshot. GPU GT0 was observed at its 2000 MHz maximum;
these observations do not establish the cause of each slowdown. In particular,
Qwen3 8B's later long-prompt decode exceeded its earlier short-prompt decode.
Keep power phases separate and treat cross-model throughput rankings as
provisional working-host observations.

Ollama reported resident accelerator allocations of 7.03 GiB for Qwen3 4B,
6.13 GiB for Coder 7B, and 9.15 GiB for Qwen3 8B. Its Gemma allocation reporting
is inconsistent with the weights. These are neither peak physical-memory
measurements nor directly comparable to the NPU process RSS; they do not support
a cross-backend memory Pareto claim.

## Repeated coding comparison protocol

The 2026-10-13/14 campaign has a four-hour compute window. The plan's frozen
Terminal-Bench tasks allow eight hours per attempt, so none can start within
that window under the plan's deadline rule. This campaign instead uses a fixed
HumanEval+ cohort and reports agent readiness separately; it supplies no
Terminal-Bench score.

The cohort is 20 of HumanEval+'s 164 tasks, sampled before observing results
with seed `20261013`, with three generations per task. EvalPlus **0.3.1** and
dataset **v0.1.10** supply the original and augmented tests. All candidates
receive the same frozen messages, temperature 0.2, top-p 0.95, and a 2048-token
completion cap; seeds `20261013` through `20261015` are requested where supported.
Model chat templates and other backend defaults remain model-specific. The
scorer uses upstream sanitization and verification, with generated programs run
in a network-isolated environment.

The [cohort, prompts, hashes, raw outputs, and scored samples](runs/2026-10-13_rugged_coding/)
make the denominator reviewable. Three samples of one task are correlated;
pass fractions here are small-cohort estimates, not full-suite leaderboard
scores or evidence that an agent can edit a repository autonomously.

The first measurements ran on battery. After AC was connected, GPU speed probes
were repeated in a [separate run](runs/2026-10-14_rugged_ac_speed/). Broad local
checks contaminated an initial Qwen speed batch; that batch is retained and
excluded. Other workstation activity and swapping continued and are recorded
with `vmstat`. These are practical working-host measurements, not clean
hardware-throughput ceilings.

## Earlier agent behavior of Gemma 4 E2B

| Turn | Tool       | Outcome                              | Decode                 | Time         |
| ---- | ---------- | ------------------------------------ | ---------------------- | ------------ |
| 1    | `read`     | ok                                   | 15 tok                 | ~0 s         |
| 2-4  | `edit`     | error, three times                   | 302-462 tok, ~22 tok/s | 13-21 s each |
| 5    | `write`    | ok, whole-file rewrite fixed the bug | 208 tok                | 9.9 s        |
| 6    | `bash`     | ok, prints `4.0`                     | 85 tok                 | 3.9 s        |
| 7    | final text | ok                                   | 398 tok                | 18.6 s       |

- The model copied `read`'s line-number prefix into `oldString` and `newString`, so `edit`
  failed every time. It recovered by rewriting the file.
- Output is verbose: a one-line final answer cost 398 tokens.
- Turn latency is dominated by decode (~21 tok/s). Context stayed at 6.9-7.8k tokens per turn.
- Not observed: the ~20k-token prompt crash recorded for Ollama earlier. The largest prompt
  sent was about 16k tokens and was aborted by the test's own timeout.

## NPU

**OpenVINO GenAI executes on the NPU with the active 1.28.0 driver.** The disabled
Nix llama.cpp service is a separate, unverified path. Two independent problems
explained the diagnostic failures:

- Library discovery: bare `npu-umd-test` failed with
  `ZE_RESULT_ERROR_UNINITIALIZED`. Supplying both Level Zero 1.28.5's library
  directory and `/run/opengl-driver/lib` yielded **55/55** selected
  driver/device/command tests passing and OpenVINO discovering `Intel(R) AI Boost`.
- Missing compiler: the OpenVINO 2026.4.1 Python wheel omits its NPU compiler
  libraries. Device discovery succeeded, but pipeline creation failed with
  `Unsupported configuration key: NPU_MAX_TILES`. Supplying both compiler libraries
  from the matching official archive allowed compilation with the original driver.
  The wheel omission is tracked in [OpenVINO #36374](https://github.com/openvinotoolkit/openvino/issues/36374).

GenAI's explicit `LLMPipeline(..., "NPU")` loaded pinned TinyLlama 1.1B INT4 and
kept `/dev/accel/accel0` open while generating. After one warmup:

| Input/output tokens | Repeats | Decode tokens/s     | Median | TTFT range |
| ------------------- | ------: | ------------------- | -----: | ---------- |
| 30 / 128            |       3 | 38.50, 38.77, 38.65 |  38.65 | 279–291 ms |
| 368 / 32            |       3 | 38.87, 39.64, 39.35 |  39.35 | 273–293 ms |

Load took 24.7 s; peak process RSS was 1.30 GiB and settled RSS 653 MiB. RSS is not
total shared accelerator memory. The static 512-token prefill budget prevents
deriving uncached prefill throughput from these similar TTFTs. This proves device
execution, not coding quality or agent suitability. The isolated 1.38.0 driver /
Level Zero 1.32.0 pair also passed 61 selected tests, but no upgrade was necessary
for the successful generation.

The instruction-tuned Qwen2.5-Coder 1.5B symmetric INT4 IR also ran on the active
driver, with an 8192-token prompt limit and 2048-token response reservation.
Across the fixed 20-task HumanEval+ cohort, three samples per task, it completed
all 60 generations: median decode **13.12 tokens/s**, TTFT **1.161 s**, and
request time **15.0 s**. Peak process RSS was **2.19 GiB**. EvalPlus 0.3.1 scored
**41/60** samples passing the original tests and **39/60 (65%)** passing both
original and extra tests. This is a small cohort estimate, not a full HumanEval+
score, and the repeated samples are not 60 independent tasks.

Synthetic prompt TTFT medians were **1.173 s** at 314 input tokens, **4.129 s**
at 2283, and **7.558 s** at 4538. These are measured latencies, not native
prefill-throughput counters. The model's pinned card identifies its source as
`Qwen/Qwen2.5-Coder-1.5B-Instruct` despite the IR repository's shorter name.
These measurements were taken on battery amid normal workstation activity;
some coding generations overlapped manifest preparation. The Qwen IR pins,
generations, and timing probes are in
[PR #10451](https://github.com/agentydragon/ducktape/pull/10451).

[PR #10443](https://github.com/agentydragon/ducktape/pull/10443) contains raw logs,
version/hash pins, and reproduction commands. It bounds the dormant llama.cpp
service to 2048 context / one slot and supplies its loader path; that service
remains disabled until tested through its own runtime and GGUF models.

## Not covered

- Coding evals and tool calling for models beyond Gemma 4 E2B; context beyond 32k tokens.
- Exposure to the cluster: both endpoints bind loopback, and the NPU's bearer-token proxy on
  `:18080` is disabled.
