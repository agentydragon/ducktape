# LLM Inference — Arc GPU (SYCL)

**Goal**: Run small LLMs locally for offline/low-latency use (shell helpers, editor
completions, summarization). Separate from cluster ollama at `ollama.allegedly.works`.

**Hardware**: Arc 130V/140V iGPU (SYCL). 30GB RAM.

## Retired (2026-09-17)

The IPEX-LLM Docker container (`intelanalytics/ipex-llm-inference-cpp-xpu`,
`podman-ipex-ollama.service`) that used to run here is gone. Intel archived
`intel/ipex-llm` on 2026-01-28 ("known security issues," no more patches
accepted); its bundled Ollama was permanently frozen at `0.9.3`, too old to
ever pull Gemma 4. Measured before retirement, for comparison: Qwen3 4B
(Q4_K_M), all 37/37 layers offloaded to SYCL, **~23 tok/s** (2026-04-18).

Live alternatives on rugged's Arc iGPU: upstream `ollama-vulkan`
(`ducktape.localLlm.ollamaUpstream` in
<nix/nixos/hosts/rugged/local_llm_arc.nix>) and Google's LiteRT-LM
(`.#litert-lm`, Vulkan backend) — see <gemma4.md>.

**Nix-native SYCL status** (this used to be why a container was needed at all):

- `services.ollama.acceleration` still only supports `"cuda"` and `"rocm"` — no
  `"intel"` option ([nixpkgs#327999](https://github.com/NixOS/nixpkgs/issues/327999), still open)
- The Intel DPC++/SYCL compiler (`intel-llvm`) **is now packaged** in nixpkgs
  ([nixpkgs#367722](https://github.com/NixOS/nixpkgs/issues/367722), closed
  via PR #470035, April 2026). A native `llama-cpp` SYCL build may now be
  possible, but nixpkgs' `llama-cpp` has no `syclSupport` flag yet — this is
  unstarted packaging work, and SYCL-vs-Vulkan performance on _integrated_
  (not discrete) Arc GPUs is unproven either way.

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
