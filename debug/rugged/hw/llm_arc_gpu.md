# LLM Inference — Arc GPU (SYCL)

**Goal**: Run small LLMs locally for offline/low-latency use (shell helpers, editor
completions, summarization). Separate from cluster ollama at `ollama.allegedly.works`.

**Hardware**: Arc 130V/140V iGPU (SYCL). 30GB RAM.

## Current setup — running

IPEX-LLM Docker container (`intelanalytics/ipex-llm-inference-cpp-xpu`) runs as
`podman-ipex-ollama.service` via `virtualisation.oci-containers`. NixOS module:
<nix/nixos/hosts/rugged/local_llm_arc.nix>.

- API at `http://localhost:11434` (OpenAI-compatible)
- Model storage: `/var/lib/local-llm/ollama`
- Qwen3 4B (Q4_K_M) installed, **~23 tok/s on Arc GPU** (2026-04-18)
- The bundled Ollama is currently `0.9.3`; Gemma 4 requires a newer Ollama.
  See <gemma4.md> for the 2026-06-05 Gemma 4 experiments.
- All 37/37 layers offloaded to SYCL GPU
- Note: `ollama ps` misreports `100% CPU` — this is an IPEX-LLM display bug.
  Confirmed GPU via `journalctl -u podman-ipex-ollama.service` (`loaded SYCL backend`,
  `offloaded 37/37 layers to GPU`).

```bash
# Pull models:
sudo podman exec ipex-ollama /llm/ollama/ollama pull qwen3:4b
# Interactive chat:
sudo podman exec -it ipex-ollama /llm/ollama/ollama run qwen3:4b
# API test:
curl http://localhost:11434/api/generate -d '{"model":"qwen3:4b","prompt":"Hello","stream":false}'
```

**NixOS native ollama blockers** (why container is needed):

- `services.ollama.acceleration` only supports `"cuda"` and `"rocm"` — no `"intel"`
  option ([nixpkgs#327999](https://github.com/NixOS/nixpkgs/issues/327999))
- Intel DPC++/SYCL compiler not in nixpkgs
  ([nixpkgs#367722](https://github.com/NixOS/nixpkgs/issues/367722))

**Good model candidates** for 30GB RAM + Arc 130V:

- Qwen3 4B — strong general reasoning, tool-calling
- Gemma 3 4B — good instruction following
- Phi-4 Mini 3.8B — code/math
- Qwen2.5-Coder 7B — code completion

## Gemma 4 status (2026-06-05)

The current IPEX/Ollama service cannot pull Gemma 4 because its bundled Ollama
server is too old. Rugged now also runs upstream `ollama-vulkan` `0.30.5` from
`nixpkgs-master` on `127.0.0.1:11436`; with `OLLAMA_IGPU_ENABLE=1`, it detects
the Lunar Lake Intel iGPU through Vulkan and can run `gemma4:e2b-it-qat`.

The best Gemma 4 path tested on rugged is Google's LiteRT-LM GPU backend via
Vulkan, with speculative decoding enabled. It is packaged locally as
`.#litert-lm`. See <gemma4.md> for commands and benchmarks.
