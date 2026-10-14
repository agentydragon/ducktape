# Rugged Intel NPU: Level Zero and OpenVINO GenAI, 2026-10-13

This is a live hardware diagnostic on rugged, not a service deployment. The active NixOS driver remained `intel-npu-driver` 1.28.0 with `level-zero` 1.28.5; `ducktape.localLlm.npu.enable` remained false. No CPU fallback is counted as NPU inference. The 1.38.0 driver was tested only through isolated Nix store paths.

Rugged was on battery throughout (`AC/online=0`, platform profile `balanced`, CPU governor `powersave`). BAT0 was at 100%; BAT1 fell from 58% to 15% over the later investigation, including the coding cohort. Results should not be interpreted as AC-powered maximum-throughput numbers.

## Inputs and provenance

- Intel NPU device: `/dev/accel/accel0`, OpenVINO full name `Intel(R) AI Boost`.
- Active driver: `/run/opengl-driver/lib/libze_intel_npu.so.1` -> `/nix/store/sa5f805axsh0anjaxswniq915v475r22-intel-npu-driver-1.28.0/lib/libze_intel_npu.so.1`; loader: `/nix/store/dh158vr3svdcbjhv12fga184h5ww8kz2-level-zero-1.28.5`.
- Isolated alternative: `/nix/store/gah4vc0lrnygv5jfpijiinhwzs80yl83-intel-npu-driver-1.38.0`; loader `/nix/store/1ihkk26wwv4sj4zhw2a7p0ljy0rd1yli-level-zero-1.32.0`; validation `/nix/store/3481gb4lm42wwjb16m5bspb7ah2fp9sw-intel-npu-driver-1.38.0-validation`.
- Temporary Python diagnostic environment: `openvino` 2026.4.1 and `openvino-genai` 2026.4.1.0, installed with `uv pip install openvino-genai huggingface-hub` into a Python 3.12 venv at `/tmp/rugged-npu-openvino-venv`. This temporary wheel environment is not a declarative operational dependency.
- Matching [official OpenVINO GenAI Ubuntu 24.04 archive](https://docs.openvino.ai/2026/get-started/install-openvino/install-openvino-genai.html): `https://storage.openvinotoolkit.org/repositories/openvino_genai/packages/2026.4.1/linux/openvino_genai_ubuntu24_2026.4.1.0_x86_64.tar.gz`; SHA-256 `b8ba7b6e5ae7899e63e5e3c7f9d022ad8a4207247521e46c507f6f3ef23ea2c5`. The two `runtime/lib/intel64/libopenvino_intel_npu_compiler*.so` files were copied to the temporary wheel's `openvino/libs` directory.
- Device-probe model: [OpenVINO/TinyLlama-1.1B-Chat-v1.0-int4-ov](https://huggingface.co/OpenVINO/TinyLlama-1.1B-Chat-v1.0-int4-ov), commit `72fd31b87453f00cb4dec2e92fa790c220d909bb`; `openvino_model.bin` SHA-256 `3776f4c54c1af5e5c4b95b6f3e62b381847e081e0d13bea3daa8f2849387cb2d`, XML SHA-256 `ded7b63d8e0a7acb55a7d7ad646711802943be48dd4a91b0069380c93844857e`.
- Coding model prepared for a separate readiness smoke: [HarmenWessels/Qwen2.5-Coder-1.5B-int4-symg128-ov](https://huggingface.co/HarmenWessels/Qwen2.5-Coder-1.5B-int4-symg128-ov), commit `d86a577edece63fe5f510cb3240f70dbc7425155`; `openvino_model.bin` SHA-256 `b077d1b2332aa5a6a148e0af008de83fc1ac98e5f1e53c95581af3a819f43f7d`, XML SHA-256 `94055e1cc5c22022077bead0a72037483a9d55409c95eda976735fd41b3bf599`.

The Qwen model is explicitly derived from **Qwen/Qwen2.5-Coder-1.5B-Instruct** according to its pinned model card (`base_model_relation: quantized`; `INT4, symmetric, group size 128`; card SHA-256 `dc8f9fe1364f87cd202923383269800ca21de01bc93ee96d3f571f918c0a466a`). The Hugging Face repository name omits `-Instruct`, but this is not the base model. Its tokenizer has the Qwen ChatML template used below.

The temporary environment can be recreated for this diagnostic with these exact package versions and the archive checksum. It is intentionally not an operational service definition:

```bash
uv venv --python 3.12 /tmp/rugged-npu-openvino-venv
UV_CACHE_DIR=/tmp/uv-rugged-npu-cache uv pip install --python /tmp/rugged-npu-openvino-venv/bin/python \
  openvino==2026.4.1 openvino-genai==2026.4.1.0 openvino-tokenizers==2026.4.1.0 \
  huggingface-hub==2.2.0
curl -fL https://storage.openvinotoolkit.org/repositories/openvino_genai/packages/2026.4.1/linux/openvino_genai_ubuntu24_2026.4.1.0_x86_64.tar.gz \
  -o /tmp/rugged-openvino-genai-2026.4.1-ubuntu24.tar.gz
echo 'b8ba7b6e5ae7899e63e5e3c7f9d022ad8a4207247521e46c507f6f3ef23ea2c5  /tmp/rugged-openvino-genai-2026.4.1-ubuntu24.tar.gz' | sha256sum -c -
mkdir -p /tmp/rugged-openvino-compiler
tar -xf /tmp/rugged-openvino-genai-2026.4.1-ubuntu24.tar.gz -C /tmp/rugged-openvino-compiler
cp /tmp/rugged-openvino-compiler/openvino_genai_ubuntu24_2026.4.1.0_x86_64/runtime/lib/intel64/libopenvino_intel_npu_compiler*.so \
  /tmp/rugged-npu-openvino-venv/lib/python3.12/site-packages/openvino/libs/
/tmp/rugged-npu-openvino-venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download('OpenVINO/TinyLlama-1.1B-Chat-v1.0-int4-ov', revision='72fd31b87453f00cb4dec2e92fa790c220d909bb', local_dir='/tmp/rugged-npu-tinyllama-int4-ov')
snapshot_download('HarmenWessels/Qwen2.5-Coder-1.5B-int4-symg128-ov', revision='d86a577edece63fe5f510cb3240f70dbc7425155', local_dir='/tmp/rugged-npu-qwen25coder-15b-sym-ov')
PY
sha256sum /tmp/rugged-npu-tinyllama-int4-ov/openvino_model.bin
sha256sum /tmp/rugged-npu-qwen25coder-15b-sym-ov/openvino_model.bin
```

## Driver and compiler diagnosis

The active driver is present in `/run/opengl-driver/lib`, but a bare `npu-umd-test --gtest_filter='Driver.*:Device.*:Command*.*'` fails at `zeInitDrivers(...)=ZE_RESULT_ERROR_UNINITIALIZED` before tests. With both the loader and driver directories on `LD_LIBRARY_PATH`, [55/55 filtered tests passed](level-zero-explicit.txt), including VPU command queues and copy tests. [The default failure](level-zero-default.txt) is reproduced. `LD_DEBUG=libs` and `strace` confirmed missing runtime library discovery. OpenVINO's [device matrix](openvino-device-matrix.txt) shows neither path alone is sufficient; both expose `['CPU', 'NPU']`.

The isolated 1.38.0 driver and 1.32.0 loader also passed [61/61 filtered tests](level-zero-138-explicit-core.txt). This validates that pair as an alternative; it does not show that an upgrade is needed.

With both library paths but the unmodified PyPI wheels, [pipeline compilation failed](genai-missing-compiler.stderr.txt) with `Unsupported configuration key: NPU_MAX_TILES`. The plugin reported `NPU_COMPILER_VERSION=0` and did not list `NPU_MAX_TILES` in supported properties. The wheel has `libopenvino_intel_npu_plugin.so` but lacks `libopenvino_intel_npu_compiler_loader.so` and `libopenvino_intel_npu_compiler.so`; this matches [the upstream Linux-wheel issue](https://github.com/openvinotoolkit/openvino/issues/36374). Supplying those libraries from the exact-version archive made `NPU_COMPILER_VERSION=524292` and `NPU_MAX_TILES_supported=True` with [1.28.0](openvino-device-128-compiler.txt) and [1.38.0](openvino-device-138-compiler.txt). The [wheel-only 1.38.0 output](openvino-device-138.txt) still lacks the compiler, ruling out a driver-version fix for that failure.

For the successful 1.28.0 run, the relevant invocation was:

```bash
LD_LIBRARY_PATH=/run/opengl-driver/lib:/nix/store/dh158vr3svdcbjhv12fga184h5ww8kz2-level-zero-1.28.5/lib:/tmp/rugged-openvino-compiler/openvino_genai_ubuntu24_2026.4.1.0_x86_64/runtime/3rdparty/tbb/lib:/nix/store/00qn3vc7r4m32c072kjnrbxd86w9slzj-zstd-1.5.7/lib:/nix/store/dqj57l1prad38vjrad4bs9d977i4q5zm-gcc-15.3.0-lib/lib \
  /tmp/rugged-npu-openvino-venv/bin/python bench.py /tmp/rugged-npu-tinyllama-int4-ov
```

`bench.py` aborts if `NPU` is not visible, passes the literal device name `NPU` to `LLMPipeline`, and records `/dev/accel/accel0` among the process file descriptors after compilation. The 1.28.0 [successful raw JSONL](genai-128-run2.stdout.jsonl) and [stderr](genai-128-run2.stderr.txt) are preserved. An initial run did complete one generation but the diagnostic script then called a PerfMetrics method absent from the installed 2026.4.1 Python binding; its [traceback](genai-128.stderr.txt) is preserved separately. The script was corrected and the full run repeated.

| Workload                    | Repeats after warmup | GenAI throughput (tokens/s)        | TTFT (ms)              |
| --------------------------- | -------------------: | ---------------------------------- | ---------------------- |
| 30 input tokens, 128 output |                    3 | 38.50, 38.77, 38.65 (median 38.65) | 291.46, 290.00, 278.71 |
| 368 input tokens, 32 output |                    3 | 38.87, 39.64, 39.35 (median 39.35) | 279.13, 293.05, 272.55 |

Model load was 24.733 s. Peak process RSS was 1,365,820 KiB during load; settled RSS after generation was 668,228 KiB. This is process RSS, not total system memory or NPU shared-memory accounting. The [OpenVINO NPU guide](https://docs.openvino.ai/2026/openvino-workflow-generative/inference-with-genai/inference-with-genai-on-npu.html) describes static-shaped prefill below the default 1,024-token chunk size, so a similar TTFT for these 512-limit prompts cannot be converted into uncached prefill tokens/s. The TinyLlama answer itself was inaccurate; this is a device and throughput probe, not a coding-quality result.

## Coding model at an 8k prompt limit

The pinned Qwen2.5-Coder 1.5B symmetric INT4 IR also compiled and generated on the **active 1.28.0 driver**, with `MAX_PROMPT_LEN=8192` and `MIN_RESPONSE_LEN=2048`. [The script](coder_probe.py) used explicit `NPU`, rejected missing NPU visibility, disabled chat-template application for a raw code-completion prompt, and recorded an open `/dev/accel/accel0` file descriptor after compilation and generation. [Raw output](qwen-coder-8k-probe.stdout.jsonl) and [stderr](qwen-coder-8k-probe.stderr.txt) are preserved. Compilation/load took 163.57 s; peak process RSS was 2,101,176 KiB and settled RSS after generation was 1,015,268 KiB. One 23-input-token, 128-output-token completion took 11.43 s; GenAI reported 1,716.74 ms TTFT and 13.17 output tokens/s. The first function body was correct, but the model continued with unrelated code and prose. This one prompt is a readiness check, not a coding-accuracy score; the fixed cohort below supplies that measure.

## Frozen HumanEval+ coding cohort

The follow-up [cohort runner](run_humaneval_cohort.py) used the same frozen EvalPlus 0.3.1 / HumanEval+ v0.1.10 20-task sample as the GPU comparison, from `cluster/docs/inference/runs/2026-10-13_rugged_coding/humaneval_plus_prompts.jsonl` (SHA-256 `6c3741d0b8be9dd9adeb29eae100372b891cb0939484b5dcfc0b973604de8cbd`). The IDs were sampled from 164 with `random.Random(20261013)` and fixed before outcomes: 4, 13, 18, 24, 26, 49, 56, 60, 83, 106, 117, 121, 142, 146, 147, 150, 152, 156, 158, 159. The exact EvalPlus OpenAI-compatible system and user messages were formatted with Qwen's own chat template, then passed to GenAI with `apply_chat_template=False` to avoid double wrapping. Settings were `temperature=0.2`, `top_p=0.95`, `max_new_tokens=2048`, and per-repeat `rng_seed=20261013/14/15`; these kwargs were accepted and at least one task produced different responses across seeds. Responses were kept **raw**, before the pinned EvalPlus `sanitize` step.

With the same `LD_LIBRARY_PATH` used for `bench.py` above, this is the equivalent cohort invocation after the frozen prompts are in the same checkout:

```bash
/tmp/rugged-npu-openvino-venv/bin/python run_humaneval_cohort.py \
  ../2026-10-13_rugged_coding/humaneval_plus_prompts.jsonl humaneval-plus-raw.jsonl --max-runs 2
/tmp/rugged-npu-openvino-venv/bin/python run_humaneval_cohort.py \
  ../2026-10-13_rugged_coding/humaneval_plus_prompts.jsonl humaneval-plus-raw.jsonl
```

The live invocation used the absolute prompt path in the harness agent's worktree, `/tmp/ducktape-worktrees/2026-10-13-rugged-harness/cluster/docs/inference/runs/2026-10-13_rugged_coding/humaneval_plus_prompts.jsonl`, and the NPU worktree's run directory for output. The runner verifies the frozen prompt-file SHA-256 and resumes by `(task_id, repeat)` from already-written rows. Its default model and cache paths are the `/tmp` locations listed above.

All **60/60** generations completed on explicit `NPU` with `/dev/accel/accel0` open, in two processes (two-response gate, then the remaining 58 from the same compiled cache). [Each unsanitized response with usage, timing, memory, seed, and zero-based repeat](humaneval-plus-raw.jsonl) is preserved, along with [gate](humaneval-plus-gate.stdout.jsonl) and [full-run](humaneval-plus-full.stdout.jsonl) progress logs and their stderr files. The full run's cached pipeline loaded in 6.96 s; its peak process RSS was **2,299,444 KiB** (~2.19 GiB), while median post-generation RSS was 526,540 KiB. Across the 60 outputs, the median was **181.5 generated tokens**, **13.124 output tokens/s** (range 10.629–14.202), **1,161 ms TTFT**, and **15.005 s wall time**; total generated tokens were 12,755 over 1,072.9 s of generation calls. These are GenAI metrics for this 1.5B instruct model on battery, not a same-model GPU-vs-NPU speed comparison. GenAI exposes finish reasons, but the in-flight capture did not record them; no finish reason is inferred from output length.

The companion frozen-cohort scorer used EvalPlus 0.3.1 `sanitize.sanitize` and `evaluate.check_correctness` against the pinned HumanEval+ v0.1.10 original and augmented tests. Of the 60 NPU responses, **41/60 passed the original HumanEval tests (68.33% pass@1)** and **39/60 passed original plus augmented tests (65.0% pass@1)**. With three samples for every one of the 20 tasks, the cohort pass@1 estimator equals the corresponding sample pass fraction. The scored summary and per-response results are in the companion coding-evaluation artifact at `cluster/docs/inference/runs/2026-10-13_rugged_coding/results/npu-qwen2.5-coder-1.5b-openvino/evalplus/`; these are a fixed 20-task cohort, not a 164-task leaderboard score. The model, quantization, chat framing, and runtime all affect quality; this score does not isolate an NPU hardware effect.

## Longer-prompt prefill latency

The separate [bounded probe](prefill_probe.py) loaded the same cached 8k-prompt Qwen pipeline and generated only 12–14 tokens per prompt, three times at each synthetic note length. [Raw measurements](prefill.stdout.jsonl) and [stderr](prefill.stderr.txt) are preserved. The first 314-token call paid a 4.18 s warmup; subsequent TTFT medians were **1.173 s** at 314 input tokens, **4.129 s** at 2,283, and **7.558 s** at 4,538. Tokenization took 1–71 ms, too little to explain the increase. Dividing the input-token deltas by the TTFT deltas gives roughly **650–670 incremental input tokens/s** over these prompt sizes, as an observed end-to-end slope, not a native prefill-throughput counter. The synthetic text is highly repetitive and the NPU prefill path changes behavior at chunk boundaries; these values should not be generalized to real agent histories without testing them.

Although the pipeline compiled with an 8,192-token prompt limit, the longest prompt actually generated in these runs was 4,538 tokens. Full-limit generation and long agent histories remain unmeasured.
