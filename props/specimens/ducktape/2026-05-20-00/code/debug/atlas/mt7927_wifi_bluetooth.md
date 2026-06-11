# MediaTek MT7927 WiFi/Bluetooth on Linux

**Date**: 2026-02-25
**Machine**: Atlas
**Kernel**: 6.17.9-1-pve

## Hardware

| Function  | Bus | ID          | Chip                    |
| --------- | --- | ----------- | ----------------------- |
| WiFi      | PCI | `14c3:7927` | MT7927                  |
| Bluetooth | USB | `0489:e13a` | MT7927 (variant 0x6639) |

Both are part of the same MediaTek combo module (Foxconn branded).

## WiFi Status: Not Working

The kernel has `mt7925e` driver but it only binds to device IDs `7925` and `0717` - not `7927`. Adding the ID via `new_id` fails.

**Upstream**: MediaTek has not released firmware or announced any plans. Users have been waiting since November 2023.

**Community**: A [research project](https://github.com/ehausig/mt7927) found MT7927 is architecturally identical to MT7925 (difference: 320MHz WiFi 7 channels), but was put on hold.

**Workarounds**:

- USB WiFi dongle
- Swap M.2 module for Intel AX210/AX211 or MediaTek MT7925

## Bluetooth Status: Patches Pending

The `btusb` driver detects the USB device but fails to initialize:

- USB ID `0489:e13a` missing from device table
- Hardware variant `0x6639` unhandled in `btmtk.c`
- Firmware contains WiFi sections that hang the chip

**Upstream**: Jean-François Marlière submitted [patches to LKML](https://www.spinics.net/lists/kernel/msg6043133.html) (2026-02-08) fixing all three issues. Pending review, expected in kernel 6.18+.

**Workarounds**:

- [mt7927-bluetooth-linux](https://github.com/NarKarapetyan93/mt7927-bluetooth-linux) DKMS module
- Arch AUR: `mediatek-mt7927-dkms`

## Recommendation

Replace the M.2 WiFi card. Intel AX210/AX211 (~$20) or MediaTek MT7925 have full Linux support.

## References

- https://github.com/openwrt/mt76/issues/927
- https://jetm.github.io/blog/posts/enabling-mt7927-bluetooth-on-linux/
- https://jetm.github.io/blog/posts/mt7927-wifi-the-missing-piece/
- https://gist.github.com/max-prtsr/2e19d74e421b60fbad30b6932772e76e
