# Rugged Intel NPU: Level Zero and OpenVINO GenAI, 2026-10-13

This is a live hardware diagnostic on rugged, not a service deployment. The active NixOS driver remained `intel-npu-driver` 1.28.0 with `level-zero` 1.28.5; `ducktape.localLlm.npu.enable` remained false. No CPU fallback is counted as NPU inference. The 1.38.0 driver was tested only through isolated Nix store paths.

## Inputs and provenance

- Intel NPU device: `/dev/accel/accel0`, OpenVINO full name `Intel(R) AI Boost`.
- Active driver: `/run/opengl-driver/lib/libze_intel_npu.so.1` -> `/nix/store/sa5f...-intel-npu-driver-1.28.0`; loader: `/nix/store/dh158vr3svdcbjhv12fga184h5ww8kz2-level-zero-1.28.5`.
- Isolated alternative: `/nix/store/gah4vc0lrnygv5jfpijiinhwzs80yl83-intel-npu-driver-1.38.0`; loader `/nix/store/1ihkk26wwv4sj4zhw2a7p0ljy0rd1yli-level-zero-1.32.0`; validation `/nix/store/3481gb4lm42wwjb16m5bspb7ah2fp9sw-intel-npu-driver-1.38.0-validation`.
- Temporary Python diagnostic environment: `openvino` 2026.4.1 and `openvino-genai` 2026.4.1.0, installed with `uv pip install openvino-genai huggingface-hub` into a Python 3.12 venv at `/tmp/rugged-npu-openvino-venv`. This temporary wheel environment is not a declarative operational dependency.
- Matching [official OpenVINO GenAI Ubuntu 24.04 archive](https://docs.openvino.ai/2026/get-started/install-openvino/install-openvino-genai.html): `https://storage.openvinotoolkit.org/repositories/openvino_genai/packages/2026.4.1/linux/openvino_genai_ubuntu24_2026.4.1.0_x86_64.tar.gz`; SHA-256 `b8ba7b6e5ae7899e63e5e3c7f9d022ad8a4207247521e46c507f6f3ef23ea2c5`. The two `runtime/lib/intel64/libopenvino_intel_npu_compiler*.so` files were copied to the temporary wheel's `openvino/libs` directory.
- Device-probe model: [OpenVINO/TinyLlama-1.1B-Chat-v1.0-int4-ov](https://huggingface.co/OpenVINO/TinyLlama-1.1B-Chat-v1.0-int4-ov), commit `72fd31b87453f00cb4dec2e92fa790c220d909bb`; `openvino_model.bin` SHA-256 `3776f4c54c1af5e5c4b95b6f3e62b381847e081e0d13bea3daa8f2849387cb2d`, XML SHA-256 `ded7b63d8e0a7acb55a7d7ad646711802943be48dd4a91b0069380c93844857e`.
- Coding model prepared for a separate readiness smoke: [HarmenWessels/Qwen2.5-Coder-1.5B-int4-symg128-ov](https://huggingface.co/HarmenWessels/Qwen2.5-Coder-1.5B-int4-symg128-ov), commit `d86a577edece63fe5f510cb3240f70dbc7425155`; `openvino_model.bin` SHA-256 `b077d1b2332aa5a6a148e0af008de83fc1ac98e5f1e53c95581af3a819f43f7d`, XML SHA-256 `94055e1cc5c22022077bead0a72037483a9d55409c95eda976735fd41b3bf599`.

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
PY
sha256sum /tmp/rugged-npu-tinyllama-int4-ov/openvino_model.bin
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

Model load was 24.733 s. Peak process RSS was 1,365,820 KiB during load; settled RSS after generation was 668,228 KiB. This is process RSS, not total system memory or NPU shared-memory accounting. The NPU guide describes static-shaped prefill, so a similar TTFT for these prompts cannot be converted into uncached prefill tokens/s. The TinyLlama answer itself was inaccurate; this is a device and throughput probe, not a coding-quality result.
