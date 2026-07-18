# MediaTek MT7927 WiFi/Bluetooth on Linux

**Machine**: Atlas (ASUS ProArt X870E-CREATOR WIFI)
**Original note**: 2026-02-25 (kernel `6.17.9-1-pve`) · **Updated**: 2026-07-18

The card is the board's onboard Wi-Fi 7 / BT combo. It is **idle on atlas** — the
active uplink is the 10G Aquantia NIC (`enp12s0`) bridged into `vmbr0`
(see `ethernet_recurring/README.md`). Nothing on atlas depends on this card.

## Hardware

| Function  | Bus | ID          | Chip                      |
| --------- | --- | ----------- | ------------------------- |
| WiFi      | PCI | `14c3:7927` | MT7927 (a.k.a. MT6639)    |
| Bluetooth | USB | `0489:e13a` | MT7927 (variant `0x6639`) |

Foxconn-branded MediaTek combo module; also sold as `14c3:6639` / `14c3:0738`.

## Upstream status (2026-07-18)

Tracking issue [openwrt/mt76#927](https://github.com/openwrt/mt76/issues/927) is
**closed as completed** (2026-05-28). Kernel is now on v7.x (`v7.1` stable, `v7.2-rc3` latest).

| Function  | Status                               | First mainline | Evidence                                                                                                                                                                                                                  |
| --------- | ------------------------------------ | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Bluetooth | ✅ merged                            | `v7.1`         | `btusb.c` `{ USB_DEVICE(0x0489, 0xe13a) }`; `btmtk.c` handles `0x6639`/MT7927, loads `mediatek/mt7927/BT_RAM_CODE_MT*_hdr.bin`. 8-patch series merged into `bluetooth-next` by Luiz Augusto von Dentz. Absent in `v6.19`. |
| WiFi      | 🟡 in `v7.2` cycle, **not released** | `v7.2-rc1+`    | `mt7925/pci.c` PCI table now lists `0x7927`, `0x6639`, `0x0738` → `MT7927_FIRMWARE_WM`. Present in `v7.2-rc3`; absent in `v7.1`, `v7.0`, `v6.19`, `v6.18`.                                                                |
| Firmware  | ❌ not in `linux-firmware`           | —              | Still sourced from the ASUS driver ZIP; `jetm`'s AUR/DKMS package auto-downloads it.                                                                                                                                      |

**Maturity**: STA-mode WiFi works, but it's not at parity — ~62% TX-throughput gap vs
Windows on 6 GHz single-link, OFDMA-driven retry issues, and Wi-Fi 7 / AP mode broken.
The out-of-tree driver ([jetm/mediatek-mt7927-dkms](https://github.com/jetm/mediatek-mt7927-dkms),
v2.8+) is ahead of mainline and still iterating.

## Can atlas use it?

atlas runs Proxmox VE 9.1.7 on kernel `6.17.13-2-pve` (see `plans/atlas_proxmox_to_nixos.md`).
Reaching MT7927 support from here:

- **Stock pve kernel — no.** WiFi needs mainline `v7.2` (still `-rc`); BT needs `v7.1`.
  Proxmox tracks Debian/Ubuntu LTS (6.x) and won't ship 7.x for a long time. Dead path.
- **DKMS on the current `6.17-pve` kernel — works today.** `jetm/mediatek-mt7927-dkms`
  supports 6.17–6.18 (v2.8 fixed the `airoha_npu` compat). Needs `pve-headers-$(uname -r)`
  plus the ASUS firmware ZIP. Inherits an out-of-tree driver with the rough edges above.
- **NixOS conversion — disproportionate.** Plan exists but atlas is firmly Proxmox; NixOS
  _stable_ won't be on `v7.2` yet either. Not worth it for an unused card.
- **Swap the M.2 E-key module for Intel AX210/AX211 or MediaTek MT7925 (~$20) — best if
  actually needed.** Socketed M.2 2230, replaceable. Full support on the _current_ pve
  kernel, zero ongoing maintenance.

## Recommendation

The card is idle on a wired host — **do nothing** unless a concrete need appears (BT
peripherals, WiFi fallback). If WiFi/BT is genuinely wanted on atlas, swap the module for
AX210/AX211; it beats waiting for a pve kernel ≥ 7.2 plus non-upstream firmware.

## References

- https://github.com/openwrt/mt76/issues/927 (closed)
- https://github.com/jetm/mediatek-mt7927-dkms (out-of-tree driver, current best source)
- https://git.kernel.org/pub/scm/linux/kernel/git/bluetooth/bluetooth-next.git/log/?qt=grep&q=MT7927
- https://jetm.github.io/blog/posts/enabling-mt7927-bluetooth-on-linux/
- https://jetm.github.io/blog/posts/mt7927-wifi-the-missing-piece/
- https://gist.github.com/max-prtsr/2e19d74e421b60fbad30b6932772e76e
