# LLM Inference — NPU (llama.cpp + OpenVINO)

**Goal**: Run small LLMs on the NPU for background/offline inference.

**Hardware**: Lunar Lake NPU (~45 TOPS int8, "Intel AI Boost").

## Current setup — working (Docker)

llama.cpp with OpenVINO backend ([PR #15307](https://github.com/ggml-org/llama.cpp/pull/15307),
March 2026). Built from source as Docker image `llama-openvino:server`.
Standard `llama-server` with OpenAI-compatible API, no custom wrappers.

**Benchmarks (2026-04-18)**, context 512:

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

### Running

```bash
# Model at ~/llm-npu-test/Llama-3.2-1B-Instruct-Q4_0.gguf
docker run --rm -d --name llama-npu \
  --device=/dev/accel --device=/dev/dri \
  -p 8080:8080 \
  -v ~/llm-npu-test:/models \
  --env=GGML_OPENVINO_DEVICE=NPU \
  llama-openvino:server \
  --no-warmup -c 512 -m /models/Llama-3.2-1B-Instruct-Q4_0.gguf

# Test
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello"}],"max_tokens":50}'
```

### Building the image

No prebuilt image available — built from source:

```bash
cd ~/llm-npu-test/llama.cpp  # cloned from b8840
docker build --target=server -t llama-openvino:server -f .devops/openvino.Dockerfile .
```

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

- Nixify as a pinned podman container service first; a fully native package needs
  the Intel OpenVINO NPU compiler library, not just nixpkgs `openvino`.
- Test larger models (Qwen 2.5 1.5B, Phi-3-mini) on NPU
- Compare NPU vs Arc GPU vs CPU on same model sizes
- Retest Gemma 4 after llama.cpp/OpenVINO backend updates
- Consider running both Arc GPU and NPU servers simultaneously (different ports,
  different model sizes)
- The `local_llm_npu` nix module with `openvino_genai` Python scripts can probably
  be simplified or removed in favor of the Docker approach

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
