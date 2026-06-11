# Dell Pro Rugged 12 Tablet (RA02260) — Hardware Hub

**Last updated**: 2026-04-18
**Kernel**: 6.19.7 | **linux-firmware**: 20260309 | **Platform**: NixOS, Intel Lunar Lake
**NixOS config**: <nix/nixos/hosts/rugged/default.nix>

## Hardware Inventory

| Component      | Device                        | PCI/USB ID  | Driver                 | Status          | Notes               |
| -------------- | ----------------------------- | ----------- | ---------------------- | --------------- | ------------------- |
| CPU            | Intel Core Ultra (Lunar Lake) | —           | —                      | Working         |                     |
| GPU            | Intel Arc 130V/140V           | `8086:64a0` | `xe`                   | Working         | Vulkan crash (GTK4) |
| Wi-Fi          | Intel Wi-Fi 7 BE201 320MHz    | `8086:a840` | `iwlwifi`/`iwlmld`     | Working         | Cosmetic WEXT warn  |
| Bluetooth      | Intel BE201 (PCIe)            | `8086:a876` | `btintel_pcie`         | **Broken**      | <bluetooth.md>      |
| Webcam         | OmniVision OV08F40 via IPU7   | `8086:645d` | `intel_ipu7` (staging) | **Partial**     | <webcam.md>         |
| NPU            | Intel Lunar Lake NPU          | `8086:643e` | `intel_vpu`            | **Needs setup** | <npu.md>            |
| Audio          | Realtek ALC3204 (HDA)         | `8086:a828` | `snd_hda_intel`        | Working         |                     |
| NVMe           | KIOXIA EG6 (DRAM-less)        | `1e0f:001b` | `nvme`                 | Working         |                     |
| Touchscreen    | eGalax EETI8082 (I2C)         | `0EEF:C005` | `hid-multitouch`       | Working         | + stylus            |
| SD Card Reader | Realtek RTS525A               | `10ec:525a` | `rtsx_pci`             | Working         |                     |
| 5G Modem       | Foxconn DW5934e (SDX72)       | `105b:e11d` | `mhi_pci_generic`      | **Partial**     | <modem.md>          |
| Fingerprint    | Broadcom BCM 58200            | `0a5c:5867` | none                   | **No driver**   |                     |
| Sensor Hub     | Intel ISH (3 sensors)         | —           | `intel_ish_ipc`        | Working         | Cosmetic warn       |
| Thunderbolt 4  | Intel Lunar Lake-M            | `8086:a831` | `xhci_hcd`             | Working         |                     |

## Issue Details

- <webcam.md> — IPU7 camera, PipeWire portal, Chrome/Zoom status
- <bluetooth.md> — Intel BE201 PCIe probe failure (ETIME)
- <modem.md> — Foxconn 5G modem, FCC unlock, Google Fi
- <npu.md> — Intel NPU setup and inference frameworks
- <llm_arc_gpu.md> — LLM inference on Arc GPU (SYCL, IPEX-LLM container)
- <llm_npu.md> — LLM inference on NPU (OpenVINO)
- <esim.md> — 5G modem eSIM provisioning, FCC unlock research, lpac commands

## Working Hardware (no action needed)

| Component                        | Notes                                                                                                    |
| -------------------------------- | -------------------------------------------------------------------------------------------------------- |
| **GPU** (`xe`)                   | DMC v2.29, GuC v70.58.0, HuC v9.4.13, GSC v104.0.5.1429. Cosmetic "selective fetch" msg.                 |
| **Wi-Fi 7** (`iwlwifi`/`iwlmld`) | FW v101. WEXT warning is from userspace apps, harmless.                                                  |
| **Audio** (ALC3204)              | Speaker, headphone, internal+headset mic. SOF modules loaded.                                            |
| **Touchscreen** (EETI8082)       | Multitouch + stylus. I2C HID.                                                                            |
| **NVMe** (KIOXIA EG6)            | Working.                                                                                                 |
| **SD Card Reader** (RTS525A)     | Working.                                                                                                 |
| **Sensor Hub** (ISH)             | 3 sensors. Cosmetic `hid_field_extract` warn (rate-limited). IIO sensor proxy enabled for auto-rotation. |
| **Thunderbolt 4**                | USB4, two root ports.                                                                                    |

## Unsupported Hardware

| Component                              | Notes                                                                                                                                                                                        |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Fingerprint** (Broadcom `0a5c:5867`) | No open-source driver. Proprietary `libfprint-2-tod1-broadcom` TOD plugin may work for similar IDs (`0a5c:5843`/`5842`) but unlikely for `5867`. Dell does not support fingerprint on Linux. |

## Related Files

- <nix/nixos/hosts/rugged/default.nix> — NixOS system configuration
- <nix/nixos/hosts/rugged/ipu7-camera.nix> — IPU7 webcam NixOS module
- <nix/nixos/hosts/rugged/local_llm_arc.nix> — Arc GPU LLM inference (IPEX-LLM container)
- <nix/nixos/hosts/rugged/local_llm_npu.nix> — NPU LLM inference (OpenVINO)
