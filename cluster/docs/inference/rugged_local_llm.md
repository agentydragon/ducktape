# Rugged local LLMs

What runs on `rugged` (Core Ultra 7 268V, 30 GiB RAM shared with the iGPU, Arc 130V/140V
iGPU, Lunar Lake NPU) with no network or cluster dependency, and how to run it. Numbers are
single runs with the model already resident, on a machine in normal desktop use: sanity
checks, not benchmarks. Host config lives in `nix/nixos/hosts/rugged/` and
`nix/home/hosts/rugged-opencode.nix`; hardware debugging in `nix/debug/rugged/hw/`.

## Backends

| Backend                                   | State (2026-10-13)                                      | Gemma 4 E2B decode | Notes                                                                                                         |
| ----------------------------------------- | ------------------------------------------------------- | -----------------: | ------------------------------------------------------------------------------------------------------------- |
| Ollama 0.40.2, Vulkan on the iGPU         | Running on `127.0.0.1:11436`, model `gemma4:e2b-it-qat` |        12-17 tok/s | Cold load 31 s. Earlier notes measured ~22 tok/s.                                                             |
| LiteRT-LM CLI 0.17.0, Vulkan on the iGPU  | Installed, no server running                            |           26 tok/s | 512-token prefill at 648 tok/s. Earlier notes measured 38-40 tok/s with speculative decoding (not re-tested). |
| LiteRT-LM CLI, CPU                        | Works                                                   |           15 tok/s | Prefill 256 tok/s.                                                                                            |
| llama.cpp OpenVINO on the NPU             | Disabled (`ducktape.localLlm.npu.enable = false`)       |                  - | Not usable, see [NPU](#npu).                                                                                  |
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

The OpenAI-compatible server (not yet run) is started per the comment in
`rugged-opencode.nix`: `litert-lm serve --host 127.0.0.1 --port 9379 --enable-speculative-decoding=true`.

### OpenCode with tool calling

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

## Agent behavior of Gemma 4 E2B

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

Not a viable LLM backend yet.

- Level Zero `zeInitDrivers` returns `ZE_RESULT_ERROR_UNINITIALIZED`. With explicit store paths
  for `level-zero` 1.28.5 and `intel-npu-driver` 1.28.0, `npu-umd-test` partly runs
  (`Driver.GetApiVersion` passes) but `ConfigurationCheck` fails, so nothing is confirmed to
  execute on the NPU.
- The driver is 1.28.0 against upstream 1.32+. The earlier Docker numbers (Llama 3.2 1B Q4_0 at
  46.7 tok/s, Qwen3 4B at 10 tok/s) used Intel's bundled driver.
- llama.cpp's OpenVINO backend silently fell back to CPU and nearly froze the machine for 3m42s
  (no `--ctx-size` cap); see `nix/debug/rugged/hw/llm_npu.md`. Re-enabling needs a driver bump
  and a hard `--ctx-size`. Gemma 4 does not work on OpenVINO (tensor-shape mismatch).

## Not covered

- Models beyond Gemma 4 E2B (Qwen3 4B/8B, Qwen2.5-Coder 7B and Gemma 4 E4B are the next candidates).
- Coding evals, and context beyond 16k tokens.
- Exposure to the cluster: both endpoints bind loopback, and the NPU's bearer-token proxy on
  `:18080` is disabled.
