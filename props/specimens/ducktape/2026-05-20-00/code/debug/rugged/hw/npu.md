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
