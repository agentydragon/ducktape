# Rugged AC-powered NPU repeat and long-context capacity probe (2026-10-14)

This is a bounded **diagnostic**, not a production configuration. It checks AC-powered NPU throughput and whether cached Gemma 4 E2B on rugged's Ollama/Vulkan runtime can allocate and actually use a 131,072-token context while keeping enough memory available for the desktop. The [canonical rugged inference assessment](../../rugged_local_llm.md) owns operational recommendations.

## Models and measurement contracts

- NPU AC repeat: the same `Qwen2.5-Coder-1.5B-Instruct` INT4 OpenVINO IR and explicit `NPU` device used in the [NPU run](../2026-10-13_rugged_npu_openvino/README.md). The six [request files](ac_speed_requests/) are byte-for-byte copies of the Qwen3 4B requests in the AC GPU speed run; different model/tokenizers mean input-token counts need not match. Each request caps output at 256 tokens. This repeats throughput under AC power without redoing the 60-sample coding evaluation.
- First long-context candidate: cached `gemma4:e2b-it-qat`, GGUF blob SHA-256 `3646b4c147cd235a44d91df1546d3b7d8e29b547dbe4e1f80856419aa455e6fd` (Q4_0). Google's [Gemma 4 model card](https://ai.google.dev/gemma/docs/core/model_card_4) declares 128k context for E2B/E4B. Local [API metadata](cached_model_metadata.json) reports 131072 tokens, 35 layers (seven full-attention and 28 sliding-window layers with a 512-token window), one KV head, and a shared-KV field of 20 layers. These architecture fields suggest substantially lower KV and prefill cost than Qwen3, but actual Ollama allocations and latency must be measured. The [Gemma prompt manifest](results/gemma4-e2b/prompt_manifest.json) uses Google's [official tokenizer](https://huggingface.co/google/gemma-4-E2B-it/blob/main/tokenizer.json), SHA-256 `cc8d3a0ce36466ccc1278bf987df5f71db1719b9ca6b4118264f45cb627bfe0f`.
- Second candidate: cached `qwen3:4b-instruct-2507-q4_K_M`, Ollama-reported model digest `0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0`, GGUF blob SHA-256 `85e4a5b7b8ef0e48af0e8658f5aaab9c2324c76c1641493f4d1e25fce54b18b9`. [Qwen's source config](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507/blob/main/config.json) declares `max_position_embeddings=262144`, which is model capability, not observed host throughput. The exact [Ollama tag](https://ollama.com/library/qwen3:4b-instruct-2507-q4_K_M) is Q4_K_M. The [Qwen prompt manifest](results/qwen3-4b/prompt_manifest.json) uses source tokenizer SHA-256 `aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4`.
- `probe.py` deterministically creates independent ~32k, ~64k, and ~124k-token user-content ledgers for either tokenizer, each with three codes at the start, middle, and end. Each manifest pins the tokenizer SHA-256, exact token counts, prompt hashes, anchor positions, and expected codes. Independent tokenization sizes the request; the Ollama [native `prompt_eval_count`](https://docs.ollama.com/api/chat) and runner logs establish what the server actually processed. A final native usage record is absent on interrupted requests.

The earlier 32k Qwen3 run allocated 4,608 MiB of **F16** KV. Ollama [documents](https://docs.ollama.com/faq) that Flash Attention can run automatically and q8_0 KV uses roughly half the memory of F16. That suggests around 9.6 GiB of Qwen3 q8 KV at 131,072 tokens, plus weights and compute buffers. A prior AC Gemma E2B runner log at 32,768 context reported 192 MiB F16 non-sliding KV (three shared layers) and 18 MiB sliding KV (12 shared layers, 1,536 cells). Its measured 131k allocation is below. Larger [context allocation increases memory](https://docs.ollama.com/context-length); advertised maximum alone does not prove this machine can load or process it.

## Safety and interpretation

Only a separate user-level Ollama 0.40.2 server on `127.0.0.1:11510` used `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=f16`, and the existing cached model. The system service was untouched. The probe verified the server PID and host before it could signal that temporary server. It sampled `MemAvailable`, memory PSI, and the runner's DRM resident-GTT counters every two seconds and terminated the temporary server if available memory dropped below 8 GiB, full memory PSI's 10-second average reached 10%, or the 08:50 UTC deadline expired. The first 8k-context load established cache type and GPU allocation; a tiny 131,072-context request tested allocation separately from long-prompt processing.

`/api/ps.context_length` and a `131072 cells` runner log show allocation, but do **not** prove a 128k prompt was processed. The runner's `task.n_tokens` shows the intended input, while per-batch progress shows what it reached before interruption. Host `MemAvailable`, Ollama's `size_vram`, process RSS, Intel Xe `drm-resident-gtt`, and Intel ivpu `drm-resident-memory` are different measures and are not summed or treated as a common memory frontier.

## Observed AC NPU throughput

The [six raw results](npu_ac_results.jsonl) used OpenVINO 2026.4.1 and GenAI 2026.4.1.0 on explicit `NPU` (`Intel(R) AI Boost`), with an open `/dev/accel/accel0` descriptor. Cached pipeline load/compile took 79.53 s. `MAX_PROMPT_LEN=8192`, `MIN_RESPONSE_LEN=256`, `max_new_tokens=256`, and greedy decoding were fixed across the six requests. The prior NPU coding run used `MIN_RESPONSE_LEN=2048` and different prompt/output conditions, so a throughput difference cannot be attributed to AC power alone.

| Source request             | NPU input tokens | Generated tokens | Median TTFT | Median OpenVINO throughput | Median wall |
| -------------------------- | ---------------: | ---------------: | ----------: | -------------------------: | ----------: |
| 384 words, three repeats   |          512–514 |         256 each |      384 ms |                45.76 tok/s |       6.0 s |
| 3,072 words, three repeats |      3,738–3,739 |         256 each |    1,833 ms |                41.10 tok/s |       8.0 s |

The NPU process's ivpu `drm-resident-memory` was 1,473,824 KiB after load, 2,097,304 KiB after short requests, and peaked at 2,143,392 KiB after long requests. Its steady RSS after generation was about 1,070,368 KiB; historical peak RSS during load was 2,398,936 KiB. The raw helper's `resident_gtt_kib: 0` on these rows denotes an absent **GTT** field, not zero accelerator memory: use the per-client `drm-resident-memory_kib` field.

## Gemma 131k allocation and prompt boundary

The cached Gemma E2B model digest was `07ea59a474013479c8b6b802bef095c40e964a1d776ba02f264c0e30e1aede0c`. The isolated runner reported Flash Attention enabled and F16 K/V. Each of these tiny load requests returned normally:

| Request                                                                    | `/api/ps.context_length` | Runner global + sliding KV | Runner Xe resident-GTT after load | Host MemAvailable after load |
| -------------------------------------------------------------------------- | -----------------------: | -------------------------: | --------------------------------: | ---------------------------: |
| [8k load](results/gemma4-e2b/load8k/results.jsonl)                         |                    8,192 |                48 + 18 MiB |                     5,212,612 KiB |               15,676,096 KiB |
| [131k load](results/gemma4-e2b/load131k/results.jsonl)                     |                  131,072 |               768 + 30 MiB |                     7,255,380 KiB |               13,779,576 KiB |
| [131k, batch 128 load](results/gemma4-e2b/load131k_batch128/results.jsonl) |                  131,072 |                768 + 9 MiB |                     5,740,784 KiB |               13,858,884 KiB |

The actual 131k runner used `-c 131072 --cache-type-k f16 --cache-type-v f16 --flash-attn on`; batch 128 additionally set `-b 128 -ub 128`. Its 131k load required 4.22 s and the tiny request processed just 12 input tokens. `size_vram` from `/api/ps` was 2,526,040,554 bytes in the first 131k load, far below the Xe runner's 7,255,380 KiB resident-GTT; it is not a measure of the process's entire resident GPU allocation.

The same deterministic ~32k ledger was then attempted twice at `num_ctx=131072`, temperature 0, seed 20261014, and 256 maximum output tokens. Its source tokenizer counted 31,976 content tokens; runner `task.n_tokens` was 31,984 including the chat wrapper; prompt SHA-256 was `331a4441256247c566462098bca583e9bc41e72ffceaf806a4dc340aa7d173fd` in both attempts.

- With default `num_batch=2048`, the runner reached 16,384 tokens in about 123 s before `vk::Device::waitForFences: ErrorDeviceLost`. The Xe kernel log records a timed-out job (`seqno=2407`) in that runner at 08:15:38 UTC; [kernel events](gemma_32k_kernel.log), [redacted coredump metadata/stack](gemma_32k_coredump_info.txt), [failed request](results/gemma4-e2b/input32k/results.jsonl), and [memory samples](results/gemma4-e2b/input32k/memory_samples.jsonl) preserve the evidence. Host `MemAvailable` never fell below 12,730,716 KiB and full memory PSI stayed at zero. This was a GPU job timeout, not the probe's memory guard.
- With `num_batch=128`, the runner command confirmed `-b 128 -ub 128` and reached 21,888 tokens in 396.48 s, passing the earlier crash point. At 08:24:45 UTC, host memory PSI full avg10 rose to 10.67% (some avg10 20.25%) while `MemAvailable` was 9,376,588 KiB. The [monitor](results/gemma4-e2b/input32k_batch128/memory_samples.jsonl) recorded `memory_pressure_threshold` and terminated **only** the isolated server. The [request](results/gemma4-e2b/input32k_batch128/results.jsonl) has no final usage or retrieval answer. No second Xe timeout appeared in the kernel log. A concurrent, local `bb run //cluster/cdk8s:generate_manifests` build invocation ran from 08:24:02.125 to 08:24:42 UTC; the corrected invocation that actually ran the generator started at 08:24:49.533 UTC, **after** the guard fired. This overlap is a host-load confound, not proof the Bazel build caused the PSI rise. This is a safety-censored run, not a successful 32k completion.

The 131k **allocation** is observed; no 32k or ~124k ledger completed. The second run's decreasing prefill rate and host pressure made a full ~124k attempt inappropriate within the shared 45-minute slot. Neither failure proves that every 128k-capable model or backend is impossible on rugged. Cached Qwen3 4B was not separately tested in this slot.

## Reproduction and raw records

`npu_ac_repeat.py` reads the [six copied requests](ac_speed_requests/) and writes an fsynced JSONL result per request. It needs the pinned OpenVINO GenAI venv and the exact `LD_LIBRARY_PATH` from the [NPU run](../2026-10-13_rugged_npu_openvino/README.md). The Gemma server was run in a managed terminal session with the existing model cache and these effective settings:

```bash
env OLLAMA_HOST=127.0.0.1:11510 OLLAMA_MODELS=/tmp/ducktape-rugged-models \
  OLLAMA_VULKAN=1 OLLAMA_IGPU_ENABLE=1 OLLAMA_NO_CLOUD=1 \
  OLLAMA_CONTEXT_LENGTH=131072 OLLAMA_FLASH_ATTENTION=1 \
  OLLAMA_KV_CACHE_TYPE=f16 OLLAMA_MAX_LOADED_MODELS=1 \
  OLLAMA_NUM_PARALLEL=1 OLLAMA_KEEP_ALIVE=10m ollama serve
```

`probe.py` used `--model gemma4:e2b-it-qat --tokenizer /tmp/rugged-gemma4-e2b-tokenizer.json --server-pid 827516 --num-ctx 131072 --minimum-available-gib 8 --deadline-utc 2026-10-14T08:50:00Z`; `--load-only`, `--targets 32000`, and then `--num-batch 128` selected the stages above. The full [runner log](ollama.full.log) records actual launch arguments, allocation, per-batch progress, and the Vulkan error. The model-specific JSONL files retain native responses and usage where a request finished; no interrupted request was counted as a completed measurement.
