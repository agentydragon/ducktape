# wyrm2 (VM 110) wedged mid-restart — host-side capture 2026-07-18 ~23:36 PDT

Sequel to <../README.md>. That note captured the **guest-side** root event (GPU1 Xid 79
"fell off the bus" at 02:09). This note captures the **host-side** end state ~21h later:
wyrm2 stuck mid-restart, and why a plain restart could not recover it.

## Symptom

`qm status 110` → `running`, but the guest was dead:

- `qm monitor 110` → `info status` → `VM status: running` (vCPUs "running" per QMP)
- `screendump` → `Error: no surface` (display device torn down)
- `qm guest cmd 110 ping` → `QEMU guest agent is not running`
- wyrm2 unreachable
- QEMU PID 3899 alive the whole 22h47m (started at host boot, `onboot: 1`), main thread
  in `poll_schedule_timeout` — process healthy, guest wedged.

## Why the restart wedged (root mechanism)

The compute RTX 5090 (host PCI **`0000:03:00.0`**, guest `02:00` / GPU1) had **fallen off
the PCIe bus** (Xid 79) and stayed off. Direct host config-space read proves it:

```text
0000:01:00.0 (display GPU): link_speed=2.5 GT/s   config[0:4]=de 10 85 2b   (alive)
0000:03:00.0 (compute GPU): link_speed=Unknown    config[0:4]=ff ff ff ff   (OFF THE BUS)
```

(The `2.5 GT/s` on `01:00.0` is idle link-speed scaling, not a fault — the AM5 platform
runs both GPU slots at x8 by design, and the card reads Gen5 `32 GT/s` once released/idle.)

All-`0xff` config space = the device is not answering PCIe cycles at all. A GPU in this
state **cannot be reset** by FLR, secondary-bus reset, PCI remove/rescan, or a VM reset —
there is nothing on the bus to reset. So when the guest attempted to reboot (to clear the
Xid 154 "Node Reboot Required" flag), QEMU/`vfio-pci` could not reset `03:00.0` during the
machine reset, and the guest hung mid-restart with no display and no agent.

**`qm stop 110` did not bring `03:00.0` back** — config space was still `ff ff ff ff` after
the VM fully stopped. This is the decisive fact: the fault is on the host PCIe link, not in
the guest. Only a **host reboot** (Xid 154 = Node Reboot Required) re-trains the link.

## PCIe error state at capture (`pcie_aer_state.txt`)

Grabbed before reboot (a reboot erases it), because the idle-failure telemetry points at a
PCIe/electrical cause:

- **The root port recorded a correctable PCIe error:** `00:01.3 DevSta: CorrErr+`.
  Correctable errors fit a _marginal physical link_ (signal integrity / seating / connector)
  — consistent with the electrical read, not with a power-draw cause. Weak on its own (only
  the summary bit, one error), but directional.
- **The dead GPU's own AER is unreadable now** (config all-`0xff`, `header type 7f`, link
  width `63`) — expected for an off-bus device; its counters read stale-zero.
- **AER _is_ active on the GPU endpoints** — the live display GPU `01:00.0` advertises the
  AER capability and exposes working `aer_dev_{correctable,nonfatal,fatal}` counters (clean
  this boot). So per-device error counts are already available on a live GPU and just need
  scraping (DCGM / `aer_dev_*` → Mimir). The **AMD Zen root ports (`00:01.1`, `00:01.3`) do
  not implement AER** — no `aer_dev_*`, only the basic `DevSta: CorrErr+` summary bit, which
  is what flagged the fault here. `pcie_ports=native` would only improve dmesg logging, not
  add root-port counters. Details in <../../gpu_lockup_20260718_followups.md>.

## Preserved telemetry

`telemetry-2026-07-{17,18}.csv.gz` — the `gpu-monitor` poller CSVs for the 07-17 escalation
cluster and the 07-18 terminal event, copied from wyrm2 (they otherwise live only on the guest
disk). They show the compute GPU at 11–13 W idle right up to the fall-off. Full 3-month archive
stays on wyrm2 at `/var/log/gpu-monitor/`.

## Recovery — status

1. `qm stop 110 --skiplock` — force-killed the wedged QEMU (done; graceful shutdown was
   impossible). `03:00.0` config **still `ff ff ff ff` after the stop** → host-side fault.
2. **Host reboot pending — operator is doing it.** Brings `03:00.0` back on the bus;
   wyrm2 auto-starts via `onboot: 1`.

**Gotcha:** a warm `systemctl reboot` may not fully power-cycle the GPU. If `03:00.0` still
reads `ff ff ff ff` after the reboot (`hexdump -n4 -C /sys/bus/pci/devices/0000:03:00.0/config`),
a full **cold power cycle** of atlas is required, and VM 110 autostart will fail until then.

See <../../gpu_lockup_20260718_followups.md> for the prevention/detection plan.
