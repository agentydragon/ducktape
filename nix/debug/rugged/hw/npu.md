# NPU — Intel Lunar Lake

**Goal**: Local AI inference (small LLMs, vision tasks).

**Current state**: Kernel driver works, `/dev/accel/accel0` exists, firmware loaded.
NixOS userspace driver enabled (`hardware.cpu.intel.npu.enable = true` in `default.nix`).
OpenVINO detects it as `Intel(R) AI Boost`.

**Smoke test**:

```bash
npu-umd-test  # bundled validation suite
```

## LLM Inference

See <llm_npu.md>. **Working**: llama.cpp with OpenVINO backend runs on NPU via Docker
(`llama-openvino:server`). Tested at **46.7 tok/s** with Llama 3.2 1B Q4_0.

## Known issues

- [nixpkgs#470638](https://github.com/NixOS/nixpkgs/issues/470638) —
  `hardware.cpu.intel.npu.enable` may not be available depending on nixpkgs pin
- NPU driver version (nixpkgs v1.28.0) may lag upstream (v1.32.0+)
- **Level Zero driver fails to initialize (2026-09-17)**: `npu-umd-test` (the
  driver package's own validation suite, unrelated to anything in this repo)
  fails at the very first API call:

  ```text
  zeInitDrivers(&drvCount, nullptr, &initDriverDesc) = ZE_RESULT_ERROR_UNINITIALIZED
  ```

  Ruled out: kernel module (`intel_vpu`) is loaded; `dmesg` shows zero
  accel/vpu errors; `/dev/accel/accel0` is `crw-rw-rw-` (world-accessible)
  and group `render` anyway. So this is a userspace Level Zero loader/driver
  problem, not a kernel or permissions issue — and since NixOS's own smoke
  test hits it identically, it blocks NPU access for anything on this host,
  not just this repo's code. Plausibly the version-lag issue above, not
  further root-caused yet. See <llm_npu.md> for the LLM-service-specific
  consequences (silent CPU fallback, disabled pending a fix).
