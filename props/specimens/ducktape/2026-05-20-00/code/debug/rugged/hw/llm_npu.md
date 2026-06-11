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

## TODO

- Nixify as a podman container service (like the Arc GPU `local_llm_arc` module)
- Test larger models (Qwen 2.5 1.5B, Phi-3-mini) on NPU
- Compare NPU vs Arc GPU vs CPU on same model sizes
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

## Dead ends encountered

1. **`optimum-intel` + `OVModelForCausalLM`**: Exports dynamic shapes, NPU compiler
   rejects them ([openvinotoolkit/openvino#34617](https://github.com/openvinotoolkit/openvino/issues/34617))
2. **`openvino_genai.LLMPipeline`**: Works but requires custom Python server wrapper,
   pip venv with missing NPU compiler `.so`, and many NixOS `LD_LIBRARY_PATH` hacks
3. **Ollama NPU**: Draft PR [#15205](https://github.com/ollama/ollama/pull/15205), not
   working yet
