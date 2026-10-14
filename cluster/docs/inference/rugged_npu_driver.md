# Rugged NPU driver bump (research, 2026-10-13)

Nothing was enabled, installed, or loaded. The only machine interaction was running `npu-umd-test` from existing `/nix/store` paths with `LD_LIBRARY_PATH` set in a single shell.

## Current vs available

| Item               | rugged now                                                                                           | Upstream / nixpkgs                                                                                                                                                                                            |
| ------------------ | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `intel-npu-driver` | 1.28.0 (nixos-26.05 pin)                                                                             | **1.38.0** in both `nixpkgs-master` (`c0cbedf1fa`) and `nixpkgs-unstable` (`7fd4004d05`), the pins already in `flake.lock`. Upstream releases: 1.33.0 (2026-06-02), 1.35.0 (2026-07-24), 1.38.0 (2026-09-11). |
| `level-zero`       | 1.28.5                                                                                               | 1.32.0 in master and unstable. Upstream 1.38.0 lists Level Zero 1.32.0 as its tested version.                                                                                                                 |
| Kernel             | 7.2.8                                                                                                | Upstream validated 1.38.0 on 7.0.x.                                                                                                                                                                           |
| Firmware           | `vpu_37xx/40xx/50xx_v1` from linux-firmware; the driver package also ships its own `firmware` output | 1.38.0 ships firmware dated 2026-08-20                                                                                                                                                                        |
| OpenVINO           | built in-repo (`nix/packages/openvino-npu.nix`, `npu-compiler-libs.nix`)                             | 1.38.0 was validated with OpenVINO 2026.3.1 and compiler `npu_ud_2026_38_rc1`                                                                                                                                 |

So **nixpkgs already has the bump**; no packaging work is needed. The bump is an override pulling `intel-npu-driver` and `level-zero` from `nixpkgs-master` into rugged's `hardware.cpu.intel.npu` stack.

## How the stack is wired (what a bump touches)

`hardware.cpu.intel.npu.enable = true` (nixos module in nixpkgs) adds:

- `pkgs.intel-npu-driver.validation` and `pkgs.level-zero` to `environment.systemPackages`
- `pkgs.intel-npu-driver.firmware` to `hardware.firmware`
- `pkgs.intel-npu-driver` to `hardware.graphics.extraPackages`, which links `libze_intel_npu.so` into `/run/opengl-driver/lib`

The module reads `pkgs.intel-npu-driver`, so a bump means overlaying `pkgs.intel-npu-driver` and `pkgs.level-zero` with the master versions, for rugged only (host scope, not class-wide).
The repo's OpenVINO packages must then be checked against the new driver, since the NPU compiler library and the driver are version-coupled.

## Important finding: the "Level Zero doesn't init" diagnosis may be an environment issue

`nix/debug/rugged/hw/npu.md` records `zeInitDrivers = ZE_RESULT_ERROR_UNINITIALIZED` with the stock 1.28.0 stack. Today the live system has `ducktape.localLlm.npu.enable = false`, so `/run/opengl-driver/lib` contains **no `libze_intel_npu.so`** at all (checked: zero matches for `npu|ze_`).

Running the 1.28.0 validation binary with the driver's own `lib/` and level-zero's `lib/` on `LD_LIBRARY_PATH`:

- `Driver.GetApiVersion` passes.
- All the `Command*`, `CommandCopy`, `CommandStress`, `Context*`, and `Device.*` tests I saw pass, including `MultipleVPUCommandBuffers` and `MultipleVPUJobs`. These execute work on the NPU.
- `Umd.ConfigurationCheck` fails, with an empty config list, so a config file the test expects is missing. That looks unrelated to execution.

So with the correct library path, **1.28.0 can already run Level Zero command queues on this NPU**. I only saw the first ~40 results and did not capture the full pass/fail count.
The earlier failure and the CPU fallback may therefore come from how the loader finds the driver (library path, ICD discovery) rather than from the 1.28.0 version.
That is a hypothesis; I did not reproduce the original failure.

## Suggested order for a later step (needs owner approval, changes system config)

1. Re-enable only `hardware.cpu.intel.npu.enable` (driver and firmware, without the `rugged-npu-llm` service), rebuild, and run `npu-umd-test` and an OpenVINO `benchmark_app -d NPU`-style check to see if the stock 1.28.0 stack now works.
2. If it does not, add the master `intel-npu-driver` 1.38.0 + `level-zero` 1.32.0 overlay for rugged and retest.
3. Before re-enabling `rugged-npu-llm`, add the explicit `--ctx-size` / `--parallel` cap noted in `llm_npu.md`, and make the service fail instead of falling back to CPU (e.g. check the log for "device NPU is not available").

## Open questions

- Whether the repo's `npu-compiler-libs` / `openvino-npu` pin works with driver 1.38.0, or must move to the compiler build upstream lists for it (`npu_ud_2026_38_rc1`).
- Whether llama.cpp's OpenVINO NPU backend loads Qwen3 at all with reasonable speed; the older Docker numbers (Llama 3.2 1B at 46.7 tok/s) are the only NPU measurements.
