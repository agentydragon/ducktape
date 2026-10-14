# Part B: candidate model speed and memory on rugged's Arc iGPU (2026-10-13)

Backend: upstream Ollama 0.40.2 Vulkan on the Arc 130V/140V, `num_ctx=32768`, one model loaded at a time, `think:false`, `num_predict=128`, single run per cell (indicative, not statistical).
Run on a separate user-level Ollama instance (port 11500, scratch model dir) so the system service was untouched; the system service's model was idle.
Method and script: see the README in this directory.

| Model (Ollama tag, quant)                     | Weights | `size_vram` at 32k ctx         | Cold load   | Decode @0.5k prompt | Decode @21k prompt |
| --------------------------------------------- | ------- | ------------------------------ | ----------- | ------------------- | ------------------ |
| `qwen3:4b-instruct-2507-q4_K_M`               | 2.5 GB  | 7.0 GiB                        | 10.9 s      | **32.1 tok/s**      | 12.3 tok/s         |
| `qwen2.5-coder:7b` (Q4_K_M)                   | 4.7 GB  | 6.1 GiB                        | 4.1 s       | 21.0 tok/s          | 14.7 tok/s         |
| `qwen3:8b` (Q4_K_M)                           | 5.2 GB  | 9.2 GiB                        | 5.0 s       | 18.9 tok/s          | 9.8 tok/s          |
| `gemma4:e4b`                                  | 6.6 GB  | 0.27 GiB reported (see caveat) | 16.1 s      | **31.1 tok/s**      | 17.7 tok/s         |
| `gemma4:e2b-it-qat` (earlier, system service) | 4.3 GB  | n/a                            | 31.5 s cold | 17 tok/s            | not measured       |

Prefill (prompt-processing) speed and time-to-answer by prompt size:

| Model                  | 0.5k tokens                 | 3.5k            | 10.5k           | 21k                 |
| ---------------------- | --------------------------- | --------------- | --------------- | ------------------- |
| qwen3-4b-instruct-2507 | 414 tok/s, 1.7 s            | 337 tok/s, 12 s | 153 tok/s, 75 s | **72 tok/s, 313 s** |
| qwen2.5-coder-7b       | 596 tok/s, 3.7 s            | 246 tok/s, 21 s | 158 tok/s, 70 s | 89 tok/s, 255 s     |
| qwen3-8b               | 415 tok/s, 2.2 s            | 269 tok/s, 16 s | 131 tok/s, 88 s | 65 tok/s, 343 s     |
| gemma4-e4b             | 104 tok/s (cold-ish), 4.9 s | 253 tok/s, 15 s | 125 tok/s, 84 s | 65 tok/s, 322 s     |

("Seconds" is wall time for the whole request including a short answer.)

## Findings

1. **No crash at ~21k context on any model.** The Ollama context crash mentioned in the plan did not reproduce with 4 models at `num_ctx=32768` and prompts up to ~21.6k tokens. I did not test above 32k or with the service's default 131072 context, so that remains open.
2. **Long context is the real cost, not decode.** Prefill falls from 400-600 tok/s at 0.5k to 65-90 tok/s at 21k (attention cost on the iGPU). A 21k-token prompt takes 4 to 6 minutes before the first token. An agent that carries 20k tokens of history without prompt caching is unusable on this backend. Caching of the prior prefix does work for follow-up turns (Part A), so the cost is mostly paid on the first large turn.
3. **Decode ranks as expected by active parameters:** the 4B-class models (Qwen3-4B, Gemma 4 E4B) hold ~31-32 tok/s at short context; the 7B-8B models are 19-21 tok/s. Qwen3-8B is the slowest and largest (9.2 GiB) and shows no obvious quality-for-speed reason to pick it for interactive agent work on this hardware.
4. **Pareto candidates on speed alone:** Qwen3-4B-Instruct-2507 and Gemma 4 E4B (both ~31 tok/s at short context). Qwen2.5-Coder-7B is the only coder-tuned model and is in the middle. Whether any of them beats Gemma 4 E2B on actual coding evals is **not measured here**; this part measures speed and memory only.
5. **Memory:** every model fits easily in 30 GiB shared RAM at 32k context (largest: 9.2 GiB). Resident host RSS stayed under 2.3 GiB because weights live in the GPU-visible pool.

## Caveats

- Single run per cell, short outputs (17-128 tokens; the decode number at long context is from 22-37 token completions for most models and is noisy). The filler prompt is random-word text, not code.
- `gemma4:e4b` reports `size_vram` 0.27 GiB, which does not match its weights; Ollama's accounting for this model (multimodal projector, per-layer embeddings kept on the host) looks off. Treat its memory number as unreliable and check host RSS/`free` in a follow-up.
- Gemma 4 E4B's first row has a low prefill number (104 tok/s) because it was measured right after the cold load.
- No quality or tool-calling measurement of the new models yet. Part A's tool-call result applies to Gemma 4 E2B only.
