# GPU Lockup Capture — 2026-07-18 (during DeepSeek-V4-Flash E9 speedup work)

Forensics preserved before recovery/reboot. Raw data: <capture.txt>. This is the same
intermittent RTX 5090 GSP lockup class as <../wyrm_gpu_lockup.md> and
<../gpu_lockup_20260417/README.md>, hit while trying the E9 `--n-cpu-moe` sweep
(<../../../cluster/docs/inference/runs/2026-07-18_e9_deepseek_v4_flash_llamacpp/README.md>).

## Environment (changed since the April investigation)

- **Kernel:** 6.18.38 · **Driver:** 595.71.05 (CUDA 13.2) — the April note was kernel
  6.16.3 / driver 580.82.09. **The newer driver did not fix the lockup.**
- **GPUs:** 2× RTX 5090 (GB202) passed through to wyrm2 (VFIO). GPU0 = display (PCI
  `01:00.0`, healthy). GPU1 = compute (PCI `02:00.0`) — this is the one that hung.
  (April note had GPU1 at `03:00`; VM topology has since changed.)

## Timeline

- The E9 Vulkan `llama-cli` **worked**: it loaded and produced coherent output
  (`[Start thinking] We need to write a Python function is_prime(n)…`) at **2.9 t/s** —
  the number in the E9 README.
- After the 32-token generation the process **spun forever in an EOF read-loop** (its log
  floods with empty `>` prompts). `-no-cnv` did not stop it entering an interactive stdin
  read against the closed heredoc stdin, so it busy-looped at **103% CPU for ~1h48m** —
  keeping the Vulkan context open on GPU1. **Secondary bug in the run harness, not the GPU.**
- **02:09:53** — kernel re-enumerates GPU1: `NVRM: GPU at PCI:0000:02:00: GPU-<uuid>` with
  `GPU Board Serial Number: 0` (a bad/zero serial — GPU came back in a degraded state).
- **02:10:14** — GSP firmware stops responding: a cascade of
  `NVRM: GPU1 _issueRpcAndWait: rpcSendMessage failed with status 0x0000000f for fn 10`
  and `GspRmFree failed … status=0x0000000f`, tripping
  `Assertion failed: (status == NV_OK) || (status == NV_ERR_GPU_IN_FULLCHIP_RESET)` in
  `rs_client.c` / `rs_server.c` / `mem.c` / `vaspace_api.c`. A `WARNING … at
nvidia/nv.c:5384 nvidia_dev_put+0xbe` fires from a _different_ PID (309398) calling
  `nvidia_close` while GPU1 is mid-reset.
- Since then: `nvidia-smi` → `Unable to determine the device handle for GPU1: Unknown
Error` / `No devices were found`. PCI config space is unreadable
  (`current_link_speed = Unknown`, `current_link_width = 63` — 63 is garbage), yet
  `enable=1`, `runtime_status=active`, `Kernel driver in use: nvidia`. Classic hard GSP
  hang: on the bus, driver bound, chip unresponsive.

## Signature (for quick future matching)

```text
NVRM: GPU1 _issueRpcAndWait: rpcSendMessage failed with status 0x0000000f for fn 10 ...
NVRM: GPU1 rpcRmApiFree_GSP: GspRmFree failed ... status=0x0000000f
NVRM: ... Assertion failed: (status == NV_OK) || (status == NV_ERR_GPU_IN_FULLCHIP_RESET)
nvidia-smi: "Unable to determine the device handle for GPU1: 0000:02:00.0: Unknown Error"
```

`status=0x0000000f` on GSP RPC `fn 10` (an RM free path) = GSP timed out; the whole GPU is
in permanent FULLCHIP_RESET and never comes back without a reset/reboot.

## State left as-is (per user: pause here)

The wedged `llama-cli` (PID 310568, State **R** — busy-spin in userspace, **not**
uninterruptible `D`, so cleanly killable) and its bash wrapper (310567) were **left
running** and GPU1 **left hung** so this capture reflects the live scene.

## Recovery (next time)

1. Kill the wedged run: `kill 310568 310567` (safe — it is spinning in userspace).
2. GPU1 is in FULLCHIP_RESET; a userspace `nvidia-smi --gpu-reset -i 1` usually cannot
   recover a GSP hang. Reliable path is a **VM reboot of wyrm2** (or host-level VFIO
   unbind/rebind of `0000:02:00.0` if the passthrough allows it — see
   <../wyrm_gpu_lockup.md>).
3. Confirm both GPUs enumerate (`nvidia-smi` lists 2×5090) before resuming E9.

## Open prevention question (the thing actually worth fixing)

These intermittent 5090 GSP hangs are the standing blocker for any long GPU job on wyrm2.
Newer driver 595.71.05 still hangs, so it is not the specific version fixed by an update.
Candidate levers to investigate (track in <../wyrm_gpu_lockup.md>): disabling GSP firmware
offload (`NVreg_EnableGpuFirmware=0`) if the open module still allows it, PCIe ASPM / power
state pinning (`nvidia-smi -pm 1`, persistence mode), Resizable-BAR / Above-4G interplay
under VFIO, and whether Vulkan vs CUDA compute paths differ in hang rate. Also fix the E9
harness EOF spin-loop so a finished run doesn't hold a GPU context indefinitely.
