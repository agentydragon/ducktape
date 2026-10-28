# Atlas network diagnostics

For the current home topology, addresses and gateway-to-switch loss investigation,
see [Home LAN](home_lan.md). Atlas's Proxmox bridge and physical interface names
must be checked on the host before applying old commands:

```bash
ip -br link
ip -br address
ip route
bridge link
cat /etc/network/interfaces
```

Adding GPUs changed PCI enumeration during the original investigation and renamed
both onboard NICs, leaving the bridge pointed at the wrong name. Match the
interface's MAC and PCI device before repairing `bridge-ports`; do not copy an
old `enp*` name or address. Reload reviewed Proxmox interface changes with
`ifreload -a` from a connection that can survive loss of the affected link.

## Physical link failures

The March 2026 Atlas incident ended when a self-crimped cable was replaced with
factory Cat6a: link returned immediately. Keep the cable, connector and switch
port in the differential even when the driver reloads successfully.

- Check `ethtool <interface>` and `ip -s link show <interface>` for carrier,
  negotiated speed and error counters; correlate `ip monitor link` with switch logs.
- The recorded internal-PHY loopback passed while external loopback failed,
  narrowing the problem toward the external path. It did not justify clearing
  every possible NIC fault.
- Driver unload/reload did not power-cycle the PHY and did not rule out the cable.
- PCI BAR warnings also appeared on working boots; they alone did not explain
  the loss of carrier.

Capture a snapshot from the repo root with
`sudo bash debug/atlas/ethernet_recurring/collect.sh`. The collector assumes
`enp11s0`/`enp12s0` and their original PCI addresses; check those first. Its output
includes both current and previous-boot NIC, bridge, PCI and kernel state.
The collector remains in `debug/` as a reproducible diagnostic tool.
