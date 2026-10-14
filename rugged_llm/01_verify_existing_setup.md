# Rugged local-LLM: verified state of the existing setup (2026-10-13)

Read-only checks plus a few harmless inference requests on the machine itself. No system config was changed.
The survey `/mnt/project-files/rugged-llm/repo_survey_2026-10-12.md` was not reachable, so everything below is checked against the repo (`nix/nixos/hosts/rugged/`, `nix/home/hosts/rugged-opencode.nix`, `nix/debug/rugged/hw/`).

## Hardware (observed)

| Item | Value                                                                                                                        |
| ---- | ---------------------------------------------------------------------------------------------------------------------------- |
| Host | `rugged`, kernel 7.2.8 (NixOS)                                                                                               |
| CPU  | Intel Core Ultra 7 268V (Lunar Lake), 8 cores / 8 threads, AVX2 + AVX-VNNI                                                   |
| RAM  | 30 GiB (shared with the iGPU), 47 GiB swap                                                                                   |
| iGPU | Arc 130V/140V `8086:64a0`, driver `xe`. Ollama reports 23.2 GiB usable "VRAM" (shared memory)                                |
| NPU  | Lunar Lake NPU `8086:643e`, driver `intel_vpu` 1.0.0, `/dev/accel/accel0` (mode 0666), firmware `vpu_37xx/40xx/50xx` present |

## Services and tools

| Component                 | State                                                                                                           | Notes                                                                                                                                                                                                                               |
| ------------------------- | --------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Ollama (upstream, Vulkan) | **Running**, `127.0.0.1:11436`, v0.40.2                                                                         | `OLLAMA_IGPU_ENABLE=1`, `OLLAMA_CONTEXT_LENGTH=131072`. One model: `gemma4:e2b-it-qat` (Q4_0, 4.6B params, ~4.3 GB). Runner `llamacpp` on `Vulkan0` ("Intel(R) Graphics (LNL)"), fully in `size_vram`. Matches `local_llm_arc.nix`. |
| LiteRT-LM                 | CLI v0.17.0 installed; no server running (nothing on `:9379`)                                                   | One model imported: `gemma4-e2b-it`. **GPU backend fails from my shell** (see below); CPU backend works.                                                                                                                            |
| NPU llama.cpp (OpenVINO)  | **Not running, disabled** (`ducktape.localLlm.npu.enable = false`)                                              | No `rugged-npu-llm` unit, nothing on `:8090` or `:18080`. Matches the repo comment.                                                                                                                                                 |
| OpenCode                  | Installed. `~/.config/opencode/opencode.json` defines providers `rugged` (Ollama) and `rugged-litert` (`:9379`) | `tool_call = false` for both models. Tool use is not validated.                                                                                                                                                                     |

## Measurements (harmless requests, single run each, indicative only)

| Backend                  | Model          | Prefill                                     | Decode                                            | Notes                                                                                                             |
| ------------------------ | -------------- | ------------------------------------------- | ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Ollama / Vulkan iGPU     | gemma4 E2B QAT | ~59 tok/s (24-token prompt, not meaningful) | **12.1 tok/s** (20 tok), **17.0 tok/s** (168 tok) | Cold model load 31.5 s; warm load 4 ms. Repo's earlier run says ~22 tok/s, so it is slower now.                   |
| LiteRT-LM / GPU (Vulkan) | gemma4-e2b-it  | **648 tok/s** (512-token prefill)           | **26.0 tok/s** (128 tok)                          | TTFT 0.83 s, init 5.7 s. Needed a workaround, see below. Repo notes 38-40 tok/s decode with speculative decoding. |
| LiteRT-LM / CPU          | gemma4-e2b-it  | 256 tok/s                                   | 15.2 tok/s                                        | TTFT 2.1 s, init 0.5 s.                                                                                           |
| NPU                      | -              | -                                           | -                                                 | Not usable, see below.                                                                                            |

Caveats: n=1, no warm-up control, Ollama's model was resident on the iGPU while LiteRT ran, and the machine was in normal desktop use. Treat these as sanity checks, not benchmarks. The harness benchmark must control for this.

## Findings

1. **Ollama on the iGPU is the one working, always-on path.** It is the only service up, and OpenCode's `rugged` provider points at it.
2. **LiteRT-LM GPU does not work out of the box in an ordinary shell.** `libvulkan.so.1` is not on the library path. Setting `LD_LIBRARY_PATH` to a 64-bit `vulkan-loader` store path plus `VK_ICD_FILENAMES=/run/opengl-driver/share/vulkan/icd.d/intel_icd.x86_64.json` makes it work.
   - The first loader I picked was 32-bit and failed with `ELFCLASS32`.
   - The packaged `litert-lm` wrapper should carry the loader. Check whether the `serve` path has the same problem. I did not start `serve`.
3. **The NPU is still not a viable LLM backend.**
   - Level Zero: the repo notes `zeInitDrivers` returns `ZE_RESULT_ERROR_UNINITIALIZED`. With explicit store paths for `level-zero` 1.28.5 and `intel-npu-driver` 1.28.0, `npu-umd-test` partly runs (`Driver.GetApiVersion` OK), but `ConfigurationCheck` fails and the `ZeInit` copy-command tests are skipped.
   - I could not tell from this whether the NPU executes anything. The earlier "falls back to CPU silently" finding and the 3m42s near-hang (no `--ctx-size` cap) in `nix/debug/rugged/hw/llm_npu.md` stand.
   - The NPU driver is 1.28.0, and upstream is 1.32+. The old Docker benchmarks (Llama 3.2 1B Q4_0 at 46.7 tok/s, Qwen3 4B at 10 tok/s) used Intel's bundled driver.
4. **Viable backends per device**

   | Device | Viable now                                     | Candidates to test                                                                            | Not viable                                  |
   | ------ | ---------------------------------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------- |
   | iGPU   | Ollama Vulkan, LiteRT-LM Vulkan                | llama.cpp Vulkan or SYCL (no nixpkgs `syclSupport`), OpenVINO GPU                             | IPEX-LLM (archived, Ollama frozen at 0.9.3) |
   | NPU    | none                                           | llama.cpp OpenVINO NPU (small, Q4_0 models only), after a driver bump and a hard `--ctx-size` | Gemma 4 on OpenVINO (tensor-shape mismatch) |
   | CPU    | LiteRT-LM CPU (15 tok/s), Ollama CPU Modelfile | llama.cpp CPU with 4B-8B models                                                               | -                                           |

5. **No agent harness has been validated against a local model.** Both OpenCode providers have `tool_call = false`. Gemma 4 E2B is a very small model for coding evals. The Pareto work needs larger candidates, e.g. Qwen3-4B/8B, Qwen2.5-Coder 7B, Gemma 4 E4B. These fit in 30 GB of shared RAM at Q4.
6. **Cluster exposure does not exist yet.** The NPU service has a bearer-token nginx proxy on `:18080`, but it is disabled. Nothing for the Ollama or LiteRT endpoints is reachable from the cluster (both bind loopback).

## Not done

- No `litert-lm serve` and no OpenCode session were started.
- No tool-calling test was run.
- No survey file comparison, since the file was unavailable.
