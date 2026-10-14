# Rugged local LLMs

What runs on `rugged` (Core Ultra 7 268V, 30 GiB RAM shared with the iGPU, Arc 130V/140V
iGPU, Lunar Lake NPU) with no network or cluster dependency, and how to run it. The earlier
Ollama speed table contains single-run sanity checks; repeated measurements below identify
their sample counts and limitations. Host config lives in `nix/nixos/hosts/rugged/` and
`nix/home/hosts/rugged-opencode.nix`; hardware debugging in `nix/debug/rugged/hw/`.

## Backends

This table preserves the initial October 13 checks. For measured long-context
capacity, see [Context capacity](#context-capacity); for repeated coding quality
and latency, see [Completed coding cohorts](#completed-coding-cohorts).

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

The NixOS service (`local_llm_arc.nix`) binds loopback only. A live check on
October 14 at 19:48 UTC found `OLLAMA_IGPU_ENABLE=1`, `OLLAMA_NO_CLOUD=1`, and
`OLLAMA_CONTEXT_LENGTH=32768`; the experimental Flash-Attention-off settings were
not yet active in that service.

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

### Local coding harness

The dedicated `rugged-opencode-offline` Home Manager command introduced by
[PR #10458](https://github.com/agentydragon/ducktape/pull/10458) uses a fresh
home directory, a minimal OpenCode configuration, and only local file and shell
tools.
The repository remains writable; Git metadata is read-only. The current launcher
connects directly to host-loopback Ollama (default port `11436`, overridable with
`RUGGED_OPENCODE_BACKEND_PORT`) and keeps host network access. A cached local model
can still be used when the host is offline, but the interactive agent itself is
not network-isolated. The separate `offline_exec` helper retains its network
namespace. Ordinary OpenCode profiles keep their existing configuration.

With the default Ollama backend, every offline-profile config exposes six
models in OpenCode's picker: five base models and the experimental 131,072-context
Gemma alias. The router-enabled profile adds Qwen3.5 0.8B as a seventh,
experimental entry. The rugged host generation prefetches all six unique base
tags; Qwen3 4B remains the default.

After activating that Home Manager change and caching the model, run it from
the project directory:

```bash
rugged-opencode-offline run "Fix the failing tests, run them, and summarize the change."
```

The experimental 128k selection in
[PR #10488](https://github.com/agentydragon/ducktape/pull/10488) uses the same launcher:

```bash
RUGGED_OPENCODE_MODEL=gemma4-e2b-long-128k-experimental rugged-opencode-offline
```

It selects the persisted alias `gemma4:e2b-it-qat-long-128k-b128` from
[PR #10491](https://github.com/agentydragon/ducktape/pull/10491). Activate both host
and Home Manager changes before using the system endpoint. The built launcher
passed a real read-tool roundtrip against a temporary server, whose actual runner
used context 131072, batch/microbatch 128, F16 KV, and Flash Attention off. The
profile reserves 4096 output tokens. This change configures the Gemma route with
2400-second LiteLLM request and stream timeouts and sets the shared Gateway's request
and backend-request ceilings to 2400 seconds. Once activated, that Gateway ceiling
applies to every authenticated LiteLLM route, not just this experiment; clients must
allow more than 40 minutes for the full request. The earlier 124,005-token smoke took
496 seconds and emitted 49 tokens at 5.8 tokens per second; at that observed decode
rate, 4096 output tokens alone would take about 706 seconds, so the previous 600-second
outer cap could not honor the configured output budget after prefill. This is a
request budget, not a quality promotion: capacity is demonstrated, but long-context
retrieval and coding quality remain experimental.

The project toolchain and dependencies must also be available locally. The
verified task uses a frozen Python standard-library test suite; this does not
establish that ducktape's usual remote Bazel workflow works without a network.

The router remains disabled by default. To prepare its cache, first activate the
host configuration with Ollama enabled; the declared prefetch unit ensures all
six unique base tags are cached: Qwen3 4B, Gemma E2B, Gemma E4B, Qwen2.5-Coder
7B, Qwen3 8B, and Qwen3.5 0.8B. The Gemma 128k alias shares the E2B base.
Router activation materializes links to those existing Ollama blobs while
keeping the blob cache read-only. It does not download models, change blob
permissions, or require manual `ollama pull` commands. The router reuses the
persistent `ollama` service identity because the cache files are created with a
restrictive umask. Switch Ollama to the mutually-exclusive router on port 11436
only after the prefetch generation has activated and all six bases are present.

When `ducktape.localLlm.llamaRouter.enable` is true, the offline OpenCode model
picker includes the six existing entries plus `Qwen3.5 0.8B Q8 (256k router
experimental; tool smoke passed)`. Select the latter explicitly with:

```bash
RUGGED_OPENCODE_MODEL=qwen3.5-0.8b-long-256k-experimental \
  rugged-opencode-offline run "..."
```

List the local choices and context/output limits with
`rugged-opencode-offline models rugged-offline --verbose`.

Its configured alias is `qwen3.5:0.8b-long-256k-b256`, with 262,144 allocated
context and a 4,096-token output budget. This is not the model's 163,840-context,
q4_0-KV/FA-on Ollama qualification: the 262k standalone-router profile has not yet
passed a long request. A tiny tool-call/result smoke passed as described below. The earlier
successful 134,993-token Ollama request used a process-wide q4_0 KV cache and
Flash Attention on; its memory profile does not transfer to this backend.

A temporary standalone llama.cpp 0.6.0 router (upstream revision b11429) was
started on a separate loopback port with all seven presets listed but unloaded.
It loaded the default Qwen3 4B alias with 32,768 context, F16 KV, Flash
Attention off, one parallel slot, reasoning off, and speculative decoding off.
A small OpenAI chat request called `read_file` for `main.py`; after a simulated
tool result, the model returned a correct summary. The request succeeded after
llama.cpp logged that object-form `tool_choice` had the wrong type and was
ignored, so this demonstrates a voluntary call and follow-up, not forced
tool-choice compatibility. This run did not exercise Gemma or Qwen3.5 presets,
sequential model eviction, or long-context processing. Available host memory
fell below the 8 GiB guard during the Qwen4 smoke; the router was stopped and no
router process or iGPU allocation remained, but available memory stayed below
the guard amid concurrent host workloads. The optional backend is therefore
kept disabled; do not switch the host until all picker profiles needed in use
have passed runtime and memory checks.

After memory recovered, a follow-up on the same standalone build completed
Gemma's 131,072-context tiny request and switched directly to Qwen3.5 0.8B at
262,144 context. Gemma returned `READY`; Qwen produced the exact
`read_file(main.py)` call with string-form `tool_choice: required` and correctly
summarized a simulated result as `main returns 42.` The respective child
arguments preserved F16 KV/Flash Attention off/batch 128 and q4_0 KV/Flash
Attention on/batch 256. Both used one slot, reasoning off, and no speculative
decoding. Each post-request model snapshot showed exactly one loaded model;
switching to Qwen evicted Gemma. Sampled minimum host available memory was
9,908,064 KiB (9.45 GiB), peak deduplicated router GPU-resident GTT was
6,484,528 KiB (6.18 GiB), and maximum full memory PSI avg10 was 18.05%.
Neither guard fired, and the router exited cleanly. These are short compatibility
and switching checks, not a completed 262k-input request or a coding benchmark.

A second short run on AC switched from Qwen3 4B to Gemma 128k and then Gemma
E4B 32k. Both Gemma profiles completed the exact `read_file(main.py)` call and
correctly reported 42 after the simulated tool result. Each model snapshot
showed one loaded model, and process samples observed at most one child.
Minimum available memory was 10.13 GiB and maximum full PSI avg10 was 6.15%;
neither guard fired. GPU memory measurements were unavailable for this run
(the sampler returned zero, which must not be interpreted as zero GPU usage).
The test router and its listener were stopped afterward. Short router checks
now cover the default Qwen3 4B, Gemma 128k, E4B 32k, and Qwen3.5 256k profiles;
the other picker profiles and long inputs still need standalone qualification.

As of the follow-up, the live host still had its older Ollama generation, only
Qwen3 4B and Gemma E2B cached, and nginx failed. Activating the merged Ollama
generation is required to populate all six bases and apply the nginx secret
ownership fix before a router switch. NixOS activation also installs the inline
Home Manager launcher. No live host activation or authenticated cluster
end-to-end verification was performed in these router checks.

The new base-tag prefetch is only cache preparation: it does not qualify a
context size or establish coding quality for Qwen3.5.

The tool-discovery failure came from combining deprecated `agent.tools` settings
with permissions: the resulting trailing wildcard denial suppressed all tools.
The dedicated profile uses permissions alone, with a wildcard denial followed by
explicit local-tool grants. Wire inspection of the built launcher confirmed six
tools, temperature 0.2, top-p 0.95, and reasoning disabled.

The archived fully isolated task run predates the current launcher networking
change. In that run, a temporary Ollama server and the built launcher both ran
inside an outer network namespace. The backend used Vulkan with all
37 Qwen3 4B layers offloaded; a public TCP connection failed with network
unreachable. The real JSONL-transform task from the frozen PR #7910 fixture ran
for about 77 seconds and exercised reads, edits, and shell tests. Its final
independent verifier passed only **2 of 4 tests**: literal Unicode output and
rejecting a string-valued `tags` field were wrong. The model claimed success,
but that claim is not counted as a successful task. The initial failure and any
verifier-guided repair are recorded separately from HumanEval+ scores.

Two attempted repair continuations selected the original workspace path even
though the launcher remaps the writable checkout to `/tmp/offline-workspace`.
Their file-access failures were harness errors and are excluded from model
quality claims. A subsequent attempt used the correct directory but was
interrupted during its edit/test loop; it is censored, not a completed failure.

The next bounded continuation completed in **178.6 seconds**, and the independent
verifier passed **4/4 original tests**, with their SHA-256 unchanged. Both backend
and agent were again network-isolated, with all 37 layers on the Vulkan device.
That archived run establishes a successful isolated coding repair with test
feedback. It is not a first-attempt success, and summing only the initial 77
seconds and this continuation would omit the intervening attempts. Full outputs, workspace
snapshots, hashes, and verifier results are retained in the
[frozen task run](runs/2026-10-13_rugged_coding/smoke_7910/).

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

- These earlier runs completed prompts up to ~21k tokens with `num_ctx=32768`.
  The later Gemma E2B long-context experiment below is separate.
- Prefill is the cost: 400-600 tok/s at 0.5k falls to 65-90 tok/s at 21k, so a 21k first prompt
  takes 4-6 minutes. Follow-up turns reuse the cache.
- These earlier runs measured speed only; the repeated coding campaign below is separate.

### Context capacity

Distinguish the model's advertised limit, the server's allocation, and an actual
completed prompt. The four Ollama candidates above completed roughly 21k-token
prompts with 32,768 total context allocated. An earlier live service default of 131,072 did not establish that it processed
a full 128k prompt; the current service default is 32,768. Input, chat formatting,
tool definitions, history, and generated output share the context budget.

The OpenCode client path was checked separately from inference in
[PR #10508](https://github.com/agentydragon/ducktape/pull/10508). Through ACP,
both requests captured by a mock backend preserved the exact 487,500-byte
fixture (123,997 tokens under the Gemma tokenizer), including its beginning,
middle, and end. They selected the 131,072-context Gemma alias with a 4,096-token
output limit. This establishes delivery of that fixture through the client;
the mock's synthetic usage counts establish neither total input tokens nor
model execution. In contrast, `opencode run --file` explicitly capped its
initial attachment read at 50 KB and instructed the agent to continue at
line 518. That is a file-reading/paging boundary, not silent server context
truncation; whether the model follows the paging instruction was not tested.

Qwen3-4B-Instruct-2507 declares
[262,144 positions](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507/blob/main/config.json).
Its observed 32k F16 KV allocation is 4608 MiB. Scaling to 128k gives 18 GiB
for F16 KV alone; Ollama's documented
[q8_0 cache reduction](https://docs.ollama.com/faq) suggests roughly 9–10 GiB
with quantization, before weights and other buffers. This is a capacity estimate,
not a successful 128k execution or a quality result. A staged experiment must
check actual cache type, accelerator placement, input counts, truncation, memory,
prefill time, and retrieval before recommending it for agent use.

Gemma 4 E2B is a more promising memory-fit candidate: its cached GGUF metadata
declares 131,072 context, seven full-attention layers, 28 sliding-window layers
with a 512-token window, one KV head, and shared KV layers. Ignoring sharing,
the architecture suggests roughly 1.75 GiB of F16 KV for full-attention layers
at 128k, plus sliding-window storage. Actual runtime allocation must still be
measured. Google's [model card](https://ai.google.dev/gemma/docs/core/model_card_4)
also declares 128k context for E2B and E4B.

The initial [long-context experiment](runs/2026-10-14_rugged_long_context/) successfully
allocated **131,072 context** for E2B with F16 KV and Flash Attention. The runner
reported 768 MiB of KV for three shared full-attention layers, plus 30 MiB for
12 sliding layers. Its sampled resident GTT was **6.92 GiB**; host available
memory remained about 13 GiB. Ollama's `size_vram` substantially underreported
that driver counter.

Allocation did not establish usable 128k inference. The first actual **31,984-token**
prompt failed during prefill: the runner logged progress to 16,384 tokens, then
aborted with Vulkan `ErrorDeviceLost`. At the same timestamp, the kernel recorded
an Intel `xe` GPU job timeout in that runner. Available memory stayed above
12.14 GiB and full memory-pressure stall average was zero; neither an OOM nor the
experiment's memory guard caused the exit. The runner used batch/microbatch 2048. A batch-128 retry passed that point and reached logged progress of 21,888
tokens, then the experiment's global memory-pressure guard stopped it. Local
Bazel build work overlapped that retry; the resulting stall pressure cannot be
attributed to inference alone. No completed 32k or 128k prompt is claimed for
these two attempts. Ollama's `/api/ps` still listed the model after the child died, so that
endpoint alone is not a successful-execution check.

Disabling Flash Attention while retaining F16 KV, batch/microbatch 128, and
131,072 total context allowed both real prompts to complete:

| Native input tokens | Cached tokens | Prefill seconds | Prefill tok/s | Time to first token | Request seconds | Exact retrieval |
| ------------------: | ------------: | --------------: | ------------: | ------------------: | --------------: | --------------: |
|              31,984 |             0 |          61.875 |         516.9 |             66.21 s |           69.22 |             3/3 |
|             124,005 |             0 |         484.679 |         255.8 |            487.67 s |          496.11 |             1/3 |

The 124k request was **not truncated** and generated 49 tokens in 8.450 seconds
(5.80 tokens/s). It recovered the middle code but corrupted the codes near the
beginning and end. Thus **128k allocation and real 124k processing work; reliable
retrieval across that window is not established**. These are single diagnostic
runs with three exact-match probes, not a long-context quality benchmark.
All layers remained on the Vulkan GPU. The 124k run had sampled peak
driver-reported resident GTT of 6,606,012 KiB (6.30 GiB), minimum host available
memory of 11,769,704 KiB (11.22 GiB), and maximum full PSI ten-second average of
11.99%. The completed 32k run peaked at 6,515,024 KiB resident GTT (6.21 GiB).

The successful runs used a revised pressure guard: stop immediately below 8 GiB
available memory, or after nine consecutive two-second samples with full PSI at
least 25%. The earlier single-sample 10% guard also censored one Flash-Attention-off
attempt despite ample available memory. Those attempts remain in the raw record;
they are not model failures or successful completions.

The result is consistent with the Intel Vulkan Flash Attention prefill problem
reported in [llama.cpp #27638](https://github.com/ggml-org/llama.cpp/issues/27638).
That issue describes slow scalar prefill paths and device loss on other Intel
hardware. We have not instrumented rugged's shader selection, so this is a
plausible explanation, not a proven identical internal cause.

Qwen3.5 provides another long-context path. Its [0.8B](https://huggingface.co/Qwen/Qwen3.5-0.8B/blob/main/config.json)
and [4B](https://huggingface.co/Qwen/Qwen3.5-4B/blob/main/config.json) models declare
262,144 positions. The following isolated Ollama 0.40.2 Vulkan runs used Flash
Attention on and an 8 GiB host-available-memory floor. The first three used AC
power; both the native-context 0.8B tiny gates and long request used battery.
A stopped request is a censored attempt, not evidence that the model rejected
the advertised context.

| Model weights | KV type | Allocated context | Native input | Observed result                                                                                                         |
| ------------- | ------- | ----------------: | -----------: | ----------------------------------------------------------------------------------------------------------------------- |
| 4B Q4_K_M     | q8_0    |           196,608 |      149,999 | Accepted; progressed through 56,320 tokens, then the host-memory guard stopped the isolated server.                     |
| 4B Q4_K_M     | q4_0    |           163,840 |      149,999 | Accepted; progressed through 24,832 tokens, then the same guard stopped it. Unrelated local builds overlapped this run. |
| 0.8B Q8_0     | q4_0    |           163,840 |      134,993 | **Completed without truncation; all three exact ledger codes recovered.**                                               |
| 0.8B Q8_0     | q4_0    |           262,144 |      209,992 | Tiny load and API gates passed; long request was operator-censored after 140,544 processed tokens. No answer.           |

The successful 0.8B request used the OpenAI-compatible `/v1/chat/completions`
endpoint with a persisted context alias and batch/microbatch 256. Its
independent tokenizer count was 134,982 content tokens; Ollama reported 134,993
prompt tokens, zero cached tokens, and `truncated = 0`. Prefill took **2,051.59 s
(34.19 minutes, 65.8 tokens/s)**, followed by 50 output tokens in 3.50 s. All
primary layers were on Vulkan. The primary model allocated 540 MiB of q4_0 KV;
its speculative draft allocated another 320 MiB of F16 KV. Sampled peak
driver-reported resident GTT was 3,356,332 KiB (3.20 GiB); minimum host available
memory was 9,661,428 KiB (9.21 GiB). The three-code check establishes that this
single >128 Ki-token request reached the model and returned the marked values;
it is not a coding-quality benchmark. The publisher's
[0.8B model card](https://huggingface.co/Qwen/Qwen3.5-0.8B#benchmark-results)
reports weaker long-context and agent scores than larger Qwen3.5 models under
different, often thinking-mode settings; this local request requested
non-thinking output and returned no reasoning field.

The later native-context run accepted 209,992 prompt tokens through `/v1` but
was canceled after 44 minutes 21 seconds of prefill because its measured tail
rate projected beyond the planned 23:15 UTC stop. It produced no answer, so its
three ledger codes were not scored; `truncated = 0` on the canceled runner slot
does not prove full processing. The 8 GiB memory guard did not fire: sampled
minimum host available memory was 8,697,956 KiB (8.30 GiB). Its primary q4_0
KV allocated 864 MiB, draft F16 KV 512 MiB, and Vulkan compute reservation
161 MiB. It used battery power throughout. This establishes native-context
allocation and partial prefill, not a completed 210k-token request.

F16-KV, Flash-Attention-off 0.8B tiny-load attempts were stopped by the same
8 GiB guard before an answer at both the model's native 262,144 context and a
163,840 context. At 262,144, primary F16 KV allocated 3,072 MiB, draft KV 512
MiB, and the runtime reserved a 3,453 MiB Vulkan compute buffer; sampled host
available memory fell to 5,602,772 KiB. At 163,840, primary KV shrank to 1,920
MiB but the compute reservation remained 3,453 MiB, and available memory fell
to 8,190,240 KiB, just below the 8 GiB floor. All primary layers were on the
GPU in both attempts. Flash Attention and KV type are process-wide Ollama 0.40.2
settings; both differ between the q4_0/Flash-Attention-on and F16/Flash-Attention-off
profiles, so the compute-buffer difference cannot be attributed to KV type alone.
The successful q4_0/Flash-Attention-on run is not qualified on
the existing Gemma F16/Flash-Attention-off service profile.

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
Terminal-Bench tasks allow eight hours per attempt; its
[next-run protocol](runs/2026-09-26_harbor_review/NEXT_RUN.md) prefers not starting
a task whose full allowance exceeds the remaining budget. This campaign chose
a fixed HumanEval+ cohort and reports agent readiness separately. It did not
start Terminal-Bench and supplies no Terminal-Bench score.

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

### Completed coding cohorts

| Candidate                               | Power   | Original tests | Original + augmented tests | Median request seconds | Median decode tok/s | Median generated tokens |
| --------------------------------------- | ------- | -------------: | -------------------------: | ---------------------: | ------------------: | ----------------------: |
| Qwen2.5-Coder 1.5B INT4, OpenVINO NPU   | Battery |          41/60 |                39/60 (65%) |                  15.00 |               13.12 |                   181.5 |
| Qwen3 4B Instruct Q4_K_M, Ollama Vulkan | AC      |          57/60 |                54/60 (90%) |                   8.50 |               28.36 |                   209.5 |
| Gemma 4 E4B, Ollama Vulkan              | AC      |          60/60 |              58/60 (96.7%) |                  19.41 |               46.32 |                     813 |

All completed 60 generations; none of their scorers recorded a test timeout.
Qwen3's generations all ended normally, rather than at the output cap. Its
20 first-repeat requests had median latency 8.67 s and native prefill duration
0.317 s. Including cached repeats lowers median prefill duration to 0.052 s;
use the varied-prefix speed probes above for uncached prefill comparisons.
Non-streaming Ollama responses do not provide an observed client TTFT.

E4B generated much longer responses, so its higher decode throughput did not
translate into shorter coding requests. Three responses hit the 2048-token cap;
all three nevertheless passed both test sets after the same upstream sanitizer.
The two augmented-test failures were on a different task. Its corrected tool
probe passed three exact `read(main.py)` calls and tool-result follow-ups; an
earlier 64-token probe cut off a long file path and is retained as a failed
protocol attempt, not counted as successful tool use.

Qwen3's sampled peak driver-reported resident GTT was **7.477 GiB**
(7,840,152 KiB), with process RSS high-water mark **1.985 GiB**. The two-second
sampler deduplicates DRM clients by ID; these counters describe different
memory views and are not added together. Sampling began after the first coding
requests, so this is not a measured initialization peak. NPU peak RSS includes
pipeline initialization; its configuration reserves 8192 input + 2048 output
tokens, versus the GPU's 32,768 total allocation.

### Published quality results (not reproduced locally)

For further candidates, the agreed validation scope is local inference, actual
context/truncation checks, tiny-N needle-in-haystack retrieval, and applicable
tool-use smokes. Needle probes check end-to-end context integrity, not model
quality: combine retrieval with native token counts and truncation logs. A failed
needle alone is not evidence that the stack truncated the prompt. Full coding benchmarks need not be rerun: published evaluations inform
expected capability, with the provenance and configuration differences retained.
The completed local cohorts above remain measurements of this setup.

| Model                  | Publisher-reported evaluation              |         Score | Source                                                                              |
| ---------------------- | ------------------------------------------ | ------------: | ----------------------------------------------------------------------------------- |
| Qwen3 4B Instruct 2507 | LiveCodeBench v6 (2025-02 through 2025-05) |          35.1 | [Qwen model card](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507#performance)   |
| Qwen3 4B Instruct 2507 | Aider-Polyglot                             |          12.9 | [Qwen model card](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507#performance)   |
| Qwen3 4B Instruct 2507 | BFCL-v3                                    |          61.9 | [Qwen model card](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507#performance)   |
| Gemma 4 E2B / E4B      | LiveCodeBench v6                           | 44.0% / 52.0% | [Google model card](https://huggingface.co/google/gemma-4-E2B-it#benchmark-results) |
| Gemma 4 E2B / E4B      | MRCR v2, eight needles, 128k average       | 19.1% / 25.4% | [Google model card](https://huggingface.co/google/gemma-4-E2B-it#benchmark-results) |

Sources checked October 14, 2026. These are publisher reports for the named
instruction-tuned models, **not reproduced rugged results**. Weight/cache
quantization, reasoning mode, runtime, sampling, prompt templates, and benchmark
subsets may differ. Our Gemma tests disable reasoning; the external table is not
a matched non-thinking experiment. Neither the published scores nor a successful
needle smoke guarantees long-context coding-agent quality. Do not combine these
scores with the local HumanEval+ fractions into one ranking.

### Observed Pareto tradeoffs

For the two completed AC GPU coding cohorts, neither candidate dominates:
Qwen3 4B has lower median request latency (8.50 versus 19.41 seconds), while
E4B passed more augmented-test samples (58 versus 54 of 60). E4B's sampled
resident GTT peak was 7.855 GiB versus Qwen's 7.477 GiB; E4B process RSS
high-water mark was 1.640 GiB. Sampling has gaps, including a deliberate pause
between E4B batches, and does not establish initialization peaks.

The NPU cohort has lower observed quality and slower requests than Qwen4 in
these runs, but it ran on battery with a different static token reservation.
It remains a separate low-memory/device option; this is not a controlled
cross-device dominance claim. No energy measurements were taken.

E2B stopped at **25/60 unscored generations** when the user prioritized long
context. Coder 7B, Qwen3 8B, and LiteRT-LM have speed/protocol measurements but
no completed coding cohort in this campaign. Consequently, the frontier above
covers only the completed comparisons. E2B's new 124k result adds a demonstrated
capacity option, with weak exact retrieval and no corresponding long-context
coding score.

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

The [AC NPU repeat](runs/2026-10-14_rugged_long_context/) sent the same six
varied-prefix speed requests used for the GPU comparison, with 256 output
tokens each:

| NPU input tokens | Repeats | Median decode tok/s | Median TTFT | Median request seconds |
| ---------------- | ------: | ------------------: | ----------: | ---------------------: |
| 512–514          |       3 |               45.76 |      384 ms |                    6.0 |
| 3738–3739        |       3 |               41.10 |     1833 ms |                    8.0 |

This pipeline reserved 8192 input + **256** response tokens, versus 2048
response tokens for the coding cohort. Load/compilation took 79.53 s. Power,
pipeline configuration, and workstation conditions all changed; the speedup
is not a measured effect of AC power alone. The NPU driver's observed resident
memory reached 2,143,392 KiB (**2.04 GiB**); peak process RSS during load was
2,398,936 KiB (**2.29 GiB**). These counters are not added together.

[PR #10443](https://github.com/agentydragon/ducktape/pull/10443) contains raw logs,
version/hash pins, and reproduction commands. It bounds the dormant llama.cpp
service to 2048 context / one slot and supplies its loader path; that service
remains disabled until tested through its own runtime and GGUF models.

## Colibri Qwen3-Coder SSD-streaming smoke

[PR #10503](https://github.com/agentydragon/ducktape/pull/10503) adds the Nix
package for Colibri 2.0.0, pinned to upstream source commit
`bf2442915d6e3dd4cdfd2eb9c2a3d2aa44a25850` (repository commit `dbebf99592`).
The tested checkpoint was `JustVugg/Qwen3-Coder-30B-A3B-colibri-int4` at
revision `d839110a17d31b4e6fa9f3d973b751f779b6b9c6`.

On 2026-10-14, a temporary loopback `coli serve` used context 1024, `--cap 8`,
one queue/KV slot, and the CPU runtime. Here `--cap 8` means eight cached
experts per layer, not 8 GiB ([launcher help at the pinned source commit](https://github.com/JustVugg/colibri/blob/bf2442915d6e3dd4cdfd2eb9c2a3d2aa44a25850/c/coli#L2226-L2232)).
The checkpoint reported 128 experts and `top_k=8`; no routing-pruning options
were set. A forced `read_file` request
returned HTTP 200 in **243.2 s**. Usage was 322 prompt and 28 completion tokens.
It generated one valid call for the exact
path below; the harness did not execute the tool or send a follow-up request,
so this verifies tool-call generation, not a completed tool roundtrip. The
scratch file contained `cedar-93017`. No decode rate is inferred from total
request time; this run has no engine phase timings. The package supports
Vulkan, but this smoke collected no GPU measurements.

```json
{
  "model": "qwen3-coder-30b-a3b-colibri",
  "messages": [
    {
      "role": "user",
      "content": "Use the read_file tool to read /tmp/colibri-smoke/marker.txt. Do not guess the contents. Make exactly one read_file tool call."
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file by absolute path.",
        "parameters": {
          "type": "object",
          "properties": { "path": { "type": "string" } },
          "required": ["path"],
          "additionalProperties": false
        }
      }
    }
  ],
  "tool_choice": { "type": "function", "function": { "name": "read_file" } },
  "max_tokens": 64,
  "temperature": 0
}
```

The startup log reported 6.1 s to load resident weights and a log-reported RSS
of 2.96 GB after load; peak RSS was not sampled. Linux `/proc/<pid>/io`
`read_bytes` was
3,393,306,624 before the request and reached 189,934,206,976 while it ran: at
least **186.5 GB** of additional physical reads. The process exited before a
final counter sample, so this is a lower bound, not a total-I/O measurement.
At this CPU/cap-8 setting, 243 seconds for a 322-token prompt and 28 output
tokens is too slow for interactive use. This smoke supports SSD-streaming
feasibility, but does not characterize other cache sizes, GPU behavior,
throughput, or agent-task quality.

## Cluster serving status

[PR #10452](https://github.com/agentydragon/ducktape/pull/10452) replaces the stale
NPU-only route with Ollama routes behind a Nebula-bound bearer proxy. It rotates
the old effectively plaintext token into a SOPS-encrypted secret shared by the
host and LiteLLM, disables Ollama cloud features, and keeps a single model and
request active at a time. The proxy allows model discovery and chat completions;
Ollama administration endpoints remain inaccessible through it.

[PR #10470](https://github.com/agentydragon/ducktape/pull/10470) is merged with
passing CI. It adds Qwen3 4B and Gemma E4B at a 32k shared context budget, advertises
tool calling, and forwards disabled reasoning through the tested LiteLLM adapter.
It supersedes the closed NPU-only draft #7112. Its initially failing manifest
check was resolved by regenerating the manifests.

Temporary nginx authentication/routing checks and generated LiteLLM adapter
checks pass, but the live proxy did not start after activation on October 14 at
08:27 UTC. Its journal reports permission denied opening the rendered bearer
configuration: the file was root-owned mode 0400, while NixOS runs both nginx's
pre-start check and master as user `nginx`. No listener was present on port 18080.

The host module now gives the rendered template to the configured nginx user
and group, retaining mode 0400. The underlying token secret remains root-only.
Nix evaluation verifies the template owner matches the service user, and a
non-root nginx configuration check passed with the actual template and a dummy
token in an owner-readable-only fixture. Applying this ownership change and
verifying a real cluster-to-rugged request remain pending; this session cannot
use passwordless sudo. These checks do not establish deployed cluster access.
