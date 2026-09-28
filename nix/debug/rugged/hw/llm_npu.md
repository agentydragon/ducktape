# LLM Inference — NPU (llama.cpp + OpenVINO)

**Goal**: Run small LLMs on the NPU for background/offline inference.

**Hardware**: Lunar Lake NPU (~45 TOPS int8, "Intel AI Boost").

## Current setup — Nix-native, currently disabled (2026-09-17)

Fully native Nix package, no container: `nix/packages/{npu-compiler-libs,openvino-npu,llama-cpp-openvino}.nix`
build a real `llama-server` against a hermetically-fetched OpenVINO NPU compiler
library, wired into a systemd service by `nix/nixos/hosts/rugged/local_llm_npu.nix`
(`ducktape.localLlm.npu`). Router mode serves both `qwen3-4b` and `llama-3.2-1b`
from one process, OpenAI-compatible API, `--sleep-idle-seconds 300` idle-unload.
Landed via #7127; **disabled** as of tonight's real-hardware test — see
"Real-hardware status" below.

Supersedes the Docker image (`llama-openvino:server`) this file used to describe
as current; that image is no longer used. Its benchmarks below remain a useful
reference point but were measured against Intel's own bundled Level Zero driver
inside the container, not NixOS's host driver package — see the status section
for why that distinction now matters.

**Benchmarks (2026-04-18, Docker image, `-c 512`)**:

| Model              | Prompt eval | Generation     |
| ------------------ | ----------- | -------------- |
| Llama 3.2 1B Q4_0  | 277 tok/s   | **46.7 tok/s** |
| Qwen 2.5 1.5B Q4_0 | 210 tok/s   | **35.0 tok/s** |
| Qwen3 4B Q4_0      | 54 tok/s    | **10 tok/s**   |

For comparison, Arc GPU (SYCL) with Qwen3 4B Q4_K_M: **~23 tok/s**.
NPU is ~2.3x slower on the same 4B model but competitive on 1-1.5B.

Gemma 4 was tested on 2026-06-05. The existing OpenVINO/Linux stack loads and
offloads Gemma 4 E2B QAT to `OPENVINO0`, but prompt compute fails with a tensor
shape mismatch. See <gemma4.md>.

## Real-hardware status (2026-09-17): disabled, NPU not actually reached

First real-hardware test of the Nix-native service (`rugged-npu-llm.service`).
Two bugs found, both real, neither yet fixed. `ducktape.localLlm.npu.enable`
is now `false` pending both.

**1. NPU device is unreachable at the Level Zero layer** (tracked in <npu.md>
§ Known issues — same root cause for anything touching the NPU on this host,
not specific to this service). The OpenVINO backend logs this and silently
continues on CPU rather than failing loudly:

```text
W GGML OpenVINO Backend: device NPU is not available, fallback to CPU
```

Directly confirmed for the `qwen3-4b` request (full journal captured the
warning). **Not directly confirmed for `llama-3.2-1b`** — the first diagnostic
pass used a grep pattern that didn't happen to include this warning text, and
by the time that was noticed the service had been restarted. Given both models
share the identical broken driver stack and `GGML_OPENVINO_DEVICE=NPU` env var,
llama-3.2-1b almost certainly also ran on CPU, not NPU — treat every inference
number produced during tonight's test as a CPU-fallback number, not a real NPU
measurement, until this is re-verified with the driver actually fixed.

**2. No explicit `--ctx-size` cap, which turned the silent CPU fallback into a
near system hang.** `local_llm_npu.nix`'s `ExecStart` sets no `-c`/`--ctx-size`,
so llama-server sized the KV cache close to the model's full trained context —
the `qwen3-4b` request loaded with `n_ctx = 198912` (of `n_ctx_train = 262144`)
across 4 parallel slots. Running that on CPU drove the process to **26.5G RSS
with a 42.1G swap peak** on this 30GiB machine — severe enough swap thrashing
that the whole machine appeared frozen (unresponsive keyboard/display) for
**3 minutes 42 seconds**, then self-recovered once the oversized allocation and
warm-up pass finished. Confirmed via `systemctl status` (swap peak, 12m33s CPU
time on the child process) and the journal timeline (model-load start to
"model loaded" spanned 3m42s). This is dangerous independent of the NPU bug —
even a working NPU path should have an explicit, bounded context size (the
"NPU constraints" section below already recommended small contexts; the
service just didn't apply that).

**Before re-enabling**: fix (1) — probably means chasing the driver-version-lag
angle in <npu.md>, or finding what differs from whatever let the old Docker
benchmarks above actually reach the NPU — and fix (2) by adding an explicit,
conservative `--ctx-size` (and probably `--parallel`) to the router presets in
`local_llm_npu.nix`, regardless of which backend ends up serving requests.

### Models

Standard GGUF files work. Q4_0 is the primary supported quantization on NPU.
Download from HuggingFace:

```bash
wget https://huggingface.co/unsloth/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_0.gguf
```

Validated models: Llama 3.2 1B, Llama 3.1 8B, Phi-3-mini, Qwen 2.5 1.5B,
Qwen3-8B, MiniCPM-1B, Mistral 7B, DeepSeek-R1-Distill-Llama-8B.

Not currently usable: Gemma 4 E2B QAT. It loads and offloads 36/36 layers to
OpenVINO, but the first prompt fails with:

```text
Can't set the input tensor with index: 3, because the model input (shape=[1,1,2,256]) and the tensor (shape=(1.35.17.256)) are incompatible
```

## TODO

- Root-cause and fix the Level Zero driver init failure (see "Real-hardware
  status" above and <npu.md> § Known issues) — the actual blocker now.
- Add an explicit `--ctx-size` cap to `local_llm_npu.nix`'s router presets
  (see "Real-hardware status" above) before ever re-enabling the service.
- Test larger models (Qwen 2.5 1.5B, Phi-3-mini) on NPU
- Compare NPU vs Arc GPU vs CPU on same model sizes
- Retest Gemma 4 after llama.cpp/OpenVINO backend updates
- Consider running both Arc GPU and NPU servers simultaneously (different ports,
  different model sizes)

## NPU constraints

- **Context**: small contexts recommended (`-c 512`), large contexts may fail
- **Quantization**: Q4_0 primary, Q4_1/Q4_K_M/Q6_K partial support
- **No model caching** on NPU yet
- **Single chat session** only with `GGML_OPENVINO_STATEFUL_EXECUTION=1`
- **No `--context-shift`** support

## Nix-native feasibility

The working `llama-openvino:server` image is not doing anything exotic at the
service layer: it runs `llama-server`, passes `/dev/accel` and `/dev/dri`, and
sets `GGML_OPENVINO_DEVICE=NPU`. That part is easy to move into the NixOS module.

The hard part is the OpenVINO payload. The Intel image uses OpenVINO
`2026.0.0.20965.c6d6a13a886` from Intel's binary bundle and contains both:

- `libopenvino_intel_npu_plugin.so`
- `libopenvino_intel_npu_compiler.so`

Nixpkgs OpenVINO was checked at pinned `2025.2.1` and nixpkgs master `2026.2.0`.
Both provide the NPU plugin and NPU headers, including the Level Zero NPU API
needed by llama.cpp, but neither provides `libopenvino_intel_npu_compiler.so`.

Practical options:

1. **Pinned OCI service**: easiest and most reproducible short term. Either keep
   using the locally built image by digest or build/pull a pinned repo image and
   wire it with `virtualisation.oci-containers`.
2. **Nix-packaged Intel bundle**: medium effort. Fetch the exact Intel OpenVINO
   tarball in Nix, expose the runtime libraries including the NPU compiler, and
   build llama.cpp's OpenVINO backend against that payload.
3. **Pure nixpkgs OpenVINO**: blocked for NPU until nixpkgs packages the NPU
   compiler library or OpenVINO no longer needs it for the tested workloads.

Update (2026-09-17): option 2 shipped, via #7127 —
`nix/packages/{npu-compiler-libs,openvino-npu,llama-cpp-openvino}.nix`. See
"Current setup" and "Real-hardware status" above for where that stands now.

## Dead ends encountered

1. **`optimum-intel` + `OVModelForCausalLM`**: Exports dynamic shapes, NPU compiler
   rejects them ([openvinotoolkit/openvino#34617](https://github.com/openvinotoolkit/openvino/issues/34617))
2. **`openvino_genai.LLMPipeline`**: Works but requires custom Python server wrapper,
   pip venv with missing NPU compiler `.so`, and many NixOS `LD_LIBRARY_PATH` hacks
3. **Ollama NPU**: Draft PR [#15205](https://github.com/ollama/ollama/pull/15205), not
   working yet
4. **LiteRT-LM generic PyPI/Nix NPU path**: Gemma 4 E2B works through LiteRT-LM
   CPU/GPU, but `--backend=npu` failed locally with "NPU is supported only for
   Intel OpenVINO on Windows." This does not rule out the newer upstream
   LiteRT-LM Intel OpenVINO path: Google now documents LunarLake-specific
   Gemma4 `.litertlm` artifacts and an Intel dispatch build. See <gemma4.md>.
