# NPU — Intel Lunar Lake

**Goal**: Local AI inference (small LLMs, vision tasks).

**Current state**: Kernel driver works, `/dev/accel/accel0` exists, firmware loaded.
NixOS userspace driver enabled (`hardware.cpu.intel.npu.enable = true` in `default.nix`).
OpenVINO detects it as `Intel(R) AI Boost`.

**Smoke test**:

```bash
LD_LIBRARY_PATH=/run/current-system/sw/lib:/run/opengl-driver/lib \
  npu-umd-test --gtest_filter='Driver.*:Device.*:Command*.*'
```

The Level Zero loader and NPU userspace driver are both needed at runtime.
The 2026-09-17 failure below was reproduced and resolved as missing library
discovery on 2026-10-13: 55/55 filtered tests passed on the active 1.28.0
driver, and OpenVINO GenAI generated on the NPU. See
<../../../../cluster/docs/inference/rugged_npu_driver.md> for the full evidence.

## LLM Inference

See <llm_npu.md>. **Working**: llama.cpp with OpenVINO backend runs on NPU via Docker
(`llama-openvino:server`). Tested at **46.7 tok/s** with Llama 3.2 1B Q4_0.

## Known issues

- [nixpkgs#470638](https://github.com/NixOS/nixpkgs/issues/470638) —
  `hardware.cpu.intel.npu.enable` may not be available depending on nixpkgs pin
- NPU driver version (nixpkgs v1.28.0) may lag upstream (v1.32.0+)
- **Historical Level Zero initialization failure (2026-09-17)**: `npu-umd-test` (the
  driver package's own validation suite, unrelated to anything in this repo)
  fails at the very first API call:

  ```text
  zeInitDrivers(&drvCount, nullptr, &initDriverDesc) = ZE_RESULT_ERROR_UNINITIALIZED
  ```

  Ruled out: kernel module (`intel_vpu`) is loaded; `dmesg` shows zero
  accel/vpu errors; `/dev/accel/accel0` is `crw-rw-rw-` (world-accessible)
  and group `render` anyway. So this is a userspace Level Zero loader/driver
  problem, not a kernel or permissions issue. Later diagnosis identified
  missing runtime library search paths, not driver-version lag. See
  <llm_npu.md> for the historical LLM-service-specific consequences.
