# Bluetooth — Intel BE201 PCIe

**Status**: Broken. `btintel_pcie 0000:00:14.7: probe with driver btintel_pcie failed with error -62`

**Device**: Intel BE201 PCIe (`8086:a876`, subsystem `8086:000e`), driver `btintel_pcie`.

## 2026-06-11 follow-up note

After several suspend/resume cycles, Bluetooth disappeared from userspace. This is
not a BlueZ or rfkill-level failure:

- `systemctl status bluetooth --no-pager`: `bluetooth.service` is active.
- `bluetoothctl list`: no controllers.
- `rfkill list`: only `0: phy0: Wireless LAN`; no Bluetooth rfkill entry.
- `lspci -nnk`: `00:14.7 Bluetooth [0d11]: Intel Corporation Device [8086:a876]`
  is still visible, but has no `Kernel driver in use`.
- `/sys/class/bluetooth` contains no `hci*` device.
- `/sys/bus/pci/devices/0000:00:14.7/uevent` reports
  `PCI_ID=8086:A876` and `MODALIAS=pci:v00008086d0000A876...`.
- Current boot (`7.0.11`) logged:
  `btintel_pcie 0000:00:14.7: probe with driver btintel_pcie failed with error -62`.
- Previous boot (`7.0.10`) did work: it logged `Bluetooth: hci0: Found device
firmware: intel/ibt-0190-0291-iml.sfi`, then
  `intel/ibt-0190-0291-pci.sfi`, then DDC load success.
- Firmware files are present under `/run/current-system/firmware/intel/`, so the
  current failure happens before firmware selection/download.
- The previous boot ended uncleanly (`last -x` shows the June 4 login ending in
  `crash` when the June 8 boot started), so a stuck device power state is still
  plausible.

Next boot triage:

1. First check whether a true cold power-off restored Bluetooth on kernel
   `7.0.11`:

   ```bash
   uname -r
   bluetoothctl list
   rfkill list
   journalctl -k -b --no-pager --grep='btintel_pcie|Bluetooth: hci0|0000:00:14.7'
   ```

   If `hci0` returns after cold power-off, the likely cause was a wedged PCIe BT
   function/device power state across an unclean warm reboot.

2. If still broken on `7.0.11`, boot the previous working NixOS generation:
   `/nix/var/nix/profiles/system-148-link` (`linux-7.0.10`,
   `nixos-system-rugged-25.11.20260526.25f5383`). Re-run the commands above.

   If `7.0.10` works and `7.0.11` fails, treat this as a kernel regression and
   pin rugged away from `pkgs.linuxPackages_latest` until a fixed kernel lands.
   The latest-kernel selection currently comes from
   `nix/nixos/hosts/rugged/ipu7-camera.nix`.

3. If both kernels fail after a cold power-off, continue with the BIOS/MSI-X
   diagnostics below.

**What we know**:

- Error -62 is `ETIME` (not `ETIMEDOUT`/-110). In the driver source, this comes from
  `btintel_pcie_enable_bt()`: the driver sets `MAC_INIT` on the controller and waits
  3000ms for a GP0 "alive" MSI-X interrupt. That interrupt never fires.
- **The failure is pre-firmware.** The device never boots to ROM stage, so firmware
  name construction (from TLV data) never happens. The `ibt-*-pci.{sfi,ddc}` files
  on disk are not relevant to this failure.
- After probe failure: PCI device `enable=0`, `BusMaster-`, no driver bound.
- `rfkill list` shows only `phy0: Wireless LAN` — Bluetooth is not listed at all,
  meaning rfkill never gets a chance to register it (probe fails before that).
- Wi-Fi (`00:14.3`, same CNVi silicon) works fine. The BT function is independently
  gated.
- All known kernel fixes (6.13 recovery mechanism, handshake sync, DSBR) are present
  in 6.19. No new `btintel_pcie` patches in kernel 7.0.
- The driver has zero module parameters — no way to increase timeout without patching.
- No physical wireless kill switch on this tablet (confirmed via Dell documentation).
  The Pro Rugged 12 has programmable buttons (P1/P2/P3) but no hardware radio slider.

**Possible causes** (not yet confirmed):

1. **BIOS has Bluetooth disabled** — Dell Rugged BIOS has independent WLAN and
   Bluetooth toggles under the Connection/Wireless menu. If BT is disabled, the
   PCI function may be powered down and unable to fire the GP0 interrupt.
2. **MSI-X delivery failure** — the interrupt vector is allocated but the platform
   isn't routing it (ACPI/IOMMU issue).
3. **Device stuck in bad power state** — the BT function never completes power-on.
4. **Firmware on the device itself is bad** — the BT controller's onboard ROM is
   corrupt or incompatible (would require fwupd/Dell firmware update).

**Diagnostic steps** (in order):

1. **Check BIOS**: Reboot → hold Volume Down (or F2 with keyboard) → look under
   "Connection" or "Wireless" for an independent Bluetooth enable/disable toggle.
   Enable it if disabled.

2. **After BIOS check, re-probe the driver**:

   ```bash
   # Remove and rescan PCI device to re-trigger probe
   sudo sh -c 'echo 1 > /sys/bus/pci/devices/0000:00:14.7/remove'
   sudo sh -c 'echo 1 > /sys/bus/pci/rescan'
   dmesg | tail -20
   ```

3. **Check MSI-X state** (requires root):

   ```bash
   sudo lspci -vvv -s 00:14.7 | grep -A5 'MSI-X\|Capabilities'
   cat /proc/interrupts | grep btintel
   ```

   If interrupt count is 0 after a probe attempt, MSI-X is not being delivered.

4. **Enable dynamic debug** (if available) for verbose probe logging:

   ```bash
   sudo sh -c 'echo "module btintel_pcie +p" > /sys/kernel/debug/dynamic_debug/control'
   sudo sh -c 'echo "module btintel +p" > /sys/kernel/debug/dynamic_debug/control'
   # Then re-probe as above
   ```

5. **Check Dell firmware updates**:

   ```bash
   fwupdmgr get-devices | grep -A5 -i bluetooth
   fwupdmgr update
   ```

6. **If all else fails** — build kernel with increased timeout
   (`BTINTEL_DEFAULT_INTR_TIMEOUT_MS` in `drivers/bluetooth/btintel_pcie.h`,
   default 3000ms → try 15000ms) to rule out a slow-boot scenario. Or file
   upstream bug with full `lspci -vvv` output for the device.

**References**:

- [btintel_pcie.c probe flow](https://github.com/torvalds/linux/blob/v6.14/drivers/bluetooth/btintel_pcie.c) — `btintel_pcie_enable_bt()` is the failing function
- [Ubuntu Bug #2085485](https://bugs.launchpad.net/ubuntu/+source/linux/+bug/2085485)
- [Dell Pro Rugged 12 Service Manual — BIOS](https://www.dell.com/support/manuals/en-us/dell-pro-ra02260-rugged-tablet/dell-pro-ruggedtab-12_ra02260_sm_a00/entering-bios-setup-program)
