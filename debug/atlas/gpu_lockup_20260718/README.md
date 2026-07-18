# GPU Lockup Capture — 2026-07-18 (during DeepSeek-V4-Flash E9 speedup work)

Forensics preserved before recovery/reboot. Raw data: <capture.txt>. Root event was an
**Xid 79 "GPU has fallen off the bus"** on the compute GPU — same intermittent RTX 5090
fall-off class as <../wyrm_gpu_lockup.md> and <../gpu_lockup_20260417/README.md>, hit while
trying the E9 `--n-cpu-moe` sweep
(<../../../cluster/docs/inference/runs/2026-07-18_e9_deepseek_v4_flash_llamacpp/README.md>).

## Environment (changed since the April investigation)

- **Kernel:** 6.18.38 · **Driver:** 595.71.05 (CUDA 13.2) — the April note was kernel
  6.16.3 / driver 580.82.09. **The newer driver did not fix the lockup.**
- **GPUs:** 2× RTX 5090 (GB202) passed through to wyrm2 (VFIO). GPU0 = display (PCI
  `01:00.0`), GPU1 = compute (PCI `02:00.0`). (April note had GPU1 at `03:00`; VM topology
  has since changed.)

## Did both GPUs lock up? (asymmetric — yes and no)

At the same millisecond both GPUs logged faults, but **only GPU1 actually died**:

- **GPU1 (02:00, compute): `Xid 79 — GPU has fallen off the bus`** (serial → 0). Hard
  failure: stopped answering the PCIe bus. This is the one now at `nvidia-smi: Unknown
Error` with unreadable config space.
- **GPU0 (01:00, display): `Xid 154 — Node Reboot Required` only, NO Xid 79.** It did not
  fall off the bus and kept rendering the display; nvidia-smi still reads it.
- Then **both** GPUs got `Xid 154` (node-reboot-required) — this looks like the driver
  propagating a node-level recovery flag after GPU1's Xid 79, not an independent GPU0
  hardware fault. Net state: GPU1 dead now, GPU0 limping but flagged reboot-required, so a
  reboot is needed to restore both.

## Timeline

- The E9 Vulkan `llama-cli` **worked**: it loaded and produced coherent output
  (`[Start thinking] We need to write a Python function is_prime(n)…`) at **2.9 t/s** —
  the number in the E9 README.
- After the 32-token generation the process **spun forever in an EOF read-loop** (its log
  floods with empty `>` prompts). `-no-cnv` did not stop it entering an interactive stdin
  read against the closed heredoc stdin, so it busy-looped at **103% CPU for ~1h48m** —
  keeping the Vulkan context open on GPU1. **Secondary bug in the run harness, not the GPU.**
- **02:09:53.351 — the primary event.** GPU1 (`02:00`) throws **`Xid 79, GPU has fallen
off the bus`**, serial → 0. Simultaneously GPU0 (`01:00`) and GPU1 both get **`Xid 154,
GPU recovery action → Node Reboot Required`**. GPU1 is gone; GPU0 keeps working (see
  "Did both GPUs lock up?" above).
- **02:10:14 — downstream cleanup failure (not the root cause).** With GPU1 already off the
  bus, the driver's resource-free path fails: a cascade of `NVRM: GPU1 _issueRpcAndWait:
rpcSendMessage failed with status 0x0000000f for fn 10` and `GspRmFree failed …
status=0x0000000f`, tripping `Assertion failed: (status == NV_OK) || (status ==
NV_ERR_GPU_IN_FULLCHIP_RESET)` in `rs_client.c` / `rs_server.c` / `mem.c` /
  `vaspace_api.c`, plus a `WARNING … at nvidia/nv.c:5384 nvidia_dev_put+0xbe` from PID
  309398 calling `nvidia_close` on the dead GPU. These are aftermath of the Xid 79, ~21 s
  later — not an independent GSP firmware hang.
- Since then: `nvidia-smi` → `Unable to determine the device handle for GPU1: Unknown
Error` / `No devices were found`. GPU1 PCI config space is unreadable
  (`current_link_speed = Unknown`, `current_link_width = 63` — 63 is garbage), yet
  `enable=1`, `runtime_status=active`, `Kernel driver in use: nvidia`: off the bus but
  still bound.

## Signature (for quick future matching)

```text
NVRM: Xid (PCI:0000:02:00): 79, GPU has fallen off the bus.        <- ROOT event
NVRM: GPU 0000:02:00.0: GPU serial number is 0.
NVRM: Xid (PCI:0000:01:00): 154, GPU recovery action ... Node Reboot Required
NVRM: Xid (PCI:0000:02:00): 154, GPU recovery action ... Node Reboot Required
NVRM: GPU1 rpcRmApiFree_GSP: GspRmFree failed ... status=0x0000000f    <- aftermath, +21s
NVRM: ... Assertion failed: (status == NV_OK) || (status == NV_ERR_GPU_IN_FULLCHIP_RESET)
nvidia-smi: "Unable to determine the device handle for GPU1: 0000:02:00.0: Unknown Error"
```

**Xid 79 (fell off the bus)** is the root: the GPU stopped answering PCIe — typically
power delivery, thermal, PCIe link/riser, or (under passthrough) host PCIe/VFIO/ASPM — not
a driver software bug. The `GspRmFree` / `FULLCHIP_RESET` storm is downstream cleanup on the
already-dead GPU. `Xid 154` is the driver's node-reboot-required flag, raised on both GPUs.

## State left as-is (per user: pause here)

The wedged `llama-cli` (PID 310568, State **R** — busy-spin in userspace, **not**
uninterruptible `D`, so cleanly killable) and its bash wrapper (310567) were **left
running** and GPU1 **left hung** so this capture reflects the live scene.

## Recovery (next time)

1. Kill the wedged run: `kill 310568 310567` (safe — it is spinning in userspace).
2. GPU1 fell off the bus; a userspace `nvidia-smi --gpu-reset -i 1` cannot recover a GPU
   that dropped off PCIe. Reliable path is a **VM reboot of wyrm2** (or host-level VFIO
   unbind/rebind of `0000:02:00.0` if the passthrough allows it — see
   <../wyrm_gpu_lockup.md>).
3. Confirm both GPUs enumerate (`nvidia-smi` lists 2×5090) before resuming E9.

## How this compares to prior wyrm2 lockups (this one is the odd one out)

Historically the wyrm2 lockups took down **both** GPUs together; today only GPU1 dropped:

| Incident                                      | GPUs                | Xid / signature                          | Trigger              |
| --------------------------------------------- | ------------------- | ---------------------------------------- | -------------------- |
| Apr 16 (<../gpu_lockup_20260417/README.md>)   | both                | none (silent, no Xid, no watchdog)       | external/unknown     |
| suspend/resume (<../wyrm_gpu_lockup.md>)      | both → FULLCHIP_RST | regs `0xBADF4100`, **Xid 119** (GSP RPC) | host suspend/resume  |
| VFIO instant-crash (`black_screen_lockup.md`) | both (2-GPU config) | crash 30–120 s after VFIO FLR reset      | Blackwell FLR @ boot |
| incident 15 (`black_screen_lockup.md`)        | one (1-GPU config)  | ZFS/pcieport stalls, **survived**        | —                    |
| **this one (Jul 18)**                         | **one (GPU1)**      | **Xid 79 (fell off the bus)** + Xid 154  | sustained compute    |

So today is **atypical**: a single card falling off the bus under load, different Xid (79
vs the historical 119/silent), and not tied to suspend/resume or a boot-time FLR reset.
That suggests a possibly _different_ failure mode (one card dropping under sustained load)
rather than a recurrence of the known both-GPU suspend/resume + FLR family — reinforcing
the power/PCIe-link-on-that-card leads below over the host-power-management path.

## Open prevention question (the thing actually worth fixing)

These intermittent 5090 "fell off the bus" (Xid 79) faults under load are the standing
blocker for any long GPU job on wyrm2. Newer driver 595.71.05 still faults, so it is not a
version fixed by an update — and Xid 79 points at **power/thermal/PCIe/VFIO**, not a driver
software bug, so the earlier "GSP firmware hang" framing was a red herring (that cascade is
downstream). Candidate levers (track in <../wyrm_gpu_lockup.md>):

- **Power delivery** — the most common Xid 79 cause under sustained load. Check PSU
  headroom for 2×575 W 5090s, 12VHPWR seating/connectors, and try a power cap
  (`nvidia-smi -pl <watts>`) to see if a lower ceiling stops the fall-offs.
- **PCIe / VFIO link stability** — ASPM disabled, `pcie_aspm=off`, fixed link speed/width;
  the garbage `current_link_width = 63` post-fault says the link itself dropped. Above-4G /
  ReBAR interplay under passthrough (see <../wyrm_gpu_lockup.md>).
- **Thermal** — log temps during a long run; a fall-off correlated with a thermal spike
  points at cooling.
- **Load correlation** — it hit during sustained Vulkan MoE compute; note whether CUDA vs
  Vulkan or lighter load changes the rate.

Also fix the E9 harness EOF spin-loop so a finished run doesn't hold a GPU context
indefinitely (independent bug, but it kept the dead context pinned for ~1h48m here).
