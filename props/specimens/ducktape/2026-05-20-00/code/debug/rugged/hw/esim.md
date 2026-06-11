# Dell Rugged 12 — 5G Modem / eSIM

## Hardware

- **Modem**: Foxconn DP25-42843-47
- **Capabilities**: GSM-UMTS, LTE, 5G NR
- **Interface**: MBIM (`/dev/wwan0mbim0`)
- **EID**: `89033023427100000000053696008750`

## NixOS Configuration

In `nix/nixos/hosts/rugged/default.nix`:

```nix
networking.modemmanager.enable = true;
programs.nm-applet.enable = true;
```

## SIM Inventory (2026-04-30)

Physical micro-SIM (Google Fi data-only kit) is in slot 1, active. eUICC
(slot 2) is empty (all profiles deleted after the eSIM throttling
investigation — see <foxflss_wwan.md>).

| Slot | Type     | ICCID                         | Provider  | Status                              |
| ---- | -------- | ----------------------------- | --------- | ----------------------------------- |
| 1    | physical | `8901240270139815559`         | Google Fi | **active**, registered, throttled\* |
| 2    | eSIM     | (none — all profiles deleted) | —         | empty                               |

\* Modem registers on Google Fi (operator `310260`) but TCP throughput
is capped at ~7.3 KB/s by carrier QoS because the modem IMEI is not
yet registered on the Fi account. ICMP and signaling are unaffected.
See `modem.md` "Lift the Google Fi QoS throttle" TODO.

Active slot is set via `mbimcli --ms-set-device-slot-mappings=0`
(0-indexed; 0 = physical, 1 = eSIM). Switch requires `systemctl stop
ModemManager` first; safe with WiFi up.

To activate a profile from a QR code (`LPA:1$<server>$<code>`):

```bash
sudo mmcli -m 0 -e --esim-activation-code='LPA:1$<server>$<code>'
```

## FCC Lock — SOLVED (2026-04-18)

The Foxconn DW5934e has an FCC lock that prevents software radio activation.
Without unlocking, ModemManager reports `power state: low` and the software radio
stays OFF.

### Root cause

The modem requires an FCC unlock handshake before the software radio can be turned on.
This must happen **every time the modem powers on** (boot, PCI rescan, resume).

### What works: FoxFlss binary

The closed-source `FoxFlss` binary from
[foxconn-pc/fii_linux](https://github.com/foxconn-pc/fii_linux) (v1.0.15) performs the
FCC unlock. It communicates via the MBIM proxy (needs ModemManager running) and reads
the system SKU via `dmidecode` to verify platform support (SKU `0D67` is supported).

**Dependencies**: only glibc + `dmidecode` on PATH.

**Sequencing** (order matters):

1. ModemManager must be running and have probed the modem (can take 20-60s after boot/PCI rescan)
2. Run `FoxFlss` (needs MBIM proxy from MM, needs `dmidecode` on PATH)
3. Restart ModemManager to pick up the new radio state (modem appears with `power state: on`)
4. Wait for MM to re-probe the modem (~20-60s)
5. Enable the modem: `mmcli -m 0 --enable`
6. Connect: `mmcli -m 0 --simple-connect="apn=h2g2,ip-type=ipv4v6"`

After step 5, the modem registers on Google Fi (5G NR, 92% signal observed).

### What doesn't work

- **libqmi DMS commands** (`--dms-foxconn-set-fcc-authentication`): SDX72 rejects all
  DMS Foxconn extensions with `WmsInvalidMessageId`. The DMS path is SDX55-only.
- **libqmi FOX service** (`--fox-set-fcc-authentication`): Needs libqmi >= 1.38.0.
  nixpkgs has 1.36.0 (only `--fox-noop` and `--fox-get-firmware-version`).
- `mbimcli --set-radio-state=on` — doesn't persist
- `rfkill unblock wwan` — WWAN not listed in rfkill
- `mmcli -m 0 --reset` — radio stays off
- ~~`fwupd`~~: Firmware already at latest (checked March 2026)

### Completed

- ~~**Declarative NixOS setup**~~: Done. See <nix/nixos/hosts/rugged/foxconn-wwan.nix>.
  FoxFlss packaged, wired as MM `fcc-unlock.d` script, declarative NM profile
  with `ipv6.never-default` and IPv4 failover (metric 1050).
- ~~**NM connection**~~: Done. Declarative "Google Fi" profile via
  `networking.networkmanager.ensureProfiles`. IPv6 `never-default` prevents
  cellular from hijacking IPv6 traffic when WiFi only has ULA addresses.

### Remaining work

- **Cold boot verification**: FCC unlock + auto-connect has only been tested
  after `nixos-rebuild switch`. Verify it works from a cold boot.
- **Suspend/resume**: `mhi_pci_suspend` returns EBUSY (-16). FoxFlss v1.0.9+
  has `--test-quick-suspend-resume` for MM, and the fii_linux repo includes
  `mm-suspend-resume-options.conf`. Needs investigation.
- **libqmi 1.38.0**: Once nixpkgs updates (or via overlay), the FCC unlock
  could use `qmicli --fox-set-fcc-authentication` instead of the closed-source
  binary. The FOX service (0xE3) works on this modem (confirmed:
  `--fox-get-firmware-version` returns `FDE2.F0.0.0.1.2.TO.003.062`).

### Google Fi APN

- APN: `h2g2`
- No username/password
- Authentication: None

## eSIM Slot Setup

The modem has two slots:

- Slot 0: Physical SIM (empty)
- Slot 1: Embedded eSIM

To switch to eSIM slot before using `lpac`:

```bash
# Stop ModemManager to release device
sudo systemctl stop ModemManager

# Switch to eSIM slot (slot 1, 0-based)
sudo nix-shell -p libmbim --run "mbimcli -d /dev/wwan0mbim0 --ms-set-device-slot-mappings=1"
```

## lpac Commands

```bash
# Environment (required for this modem)
export LPAC_APDU=mbim
export LPAC_APDU_MBIM_DEVICE=/dev/wwan0mbim0
export LPAC_APDU_MBIM_UIM_SLOT=2      # 1-based: 2 = slot index 1 (eSIM)
export LPAC_APDU_MBIM_USE_PROXY=1     # required for this modem

# Chip info
sudo nix-shell -p lpac --run 'LPAC_APDU=mbim LPAC_APDU_MBIM_DEVICE=/dev/wwan0mbim0 LPAC_APDU_MBIM_UIM_SLOT=2 LPAC_APDU_MBIM_USE_PROXY=1 lpac chip info'

# List profiles
sudo nix-shell -p lpac --run 'LPAC_APDU=mbim LPAC_APDU_MBIM_DEVICE=/dev/wwan0mbim0 LPAC_APDU_MBIM_UIM_SLOT=2 LPAC_APDU_MBIM_USE_PROXY=1 lpac profile list'

# Download profile (from QR code: LPA:1$server$code)
sudo nix-shell -p lpac --run 'LPAC_APDU=mbim ... lpac profile download -s <server> -m <matching-id>'

# Enable profile
sudo nix-shell -p lpac --run 'LPAC_APDU=mbim ... lpac profile enable <iccid>'
```

Decode a QR code to get the activation string:

```bash
nix-shell -p zbar --run "zbarimg --raw /path/to/qrcode.png"
```

## Useful Diagnostics

```bash
# ModemManager
mmcli -L
mmcli -m 0

# MBIM
sudo nix-shell -p libmbim --run "mbimcli -d /dev/wwan0mbim0 --query-device-caps"
sudo nix-shell -p libmbim --run "mbimcli -d /dev/wwan0mbim0 --query-radio-state"
sudo nix-shell -p libmbim --run "mbimcli -d /dev/wwan0mbim0 --ms-query-device-slot-mappings"
sudo nix-shell -p libmbim --run "mbimcli -d /dev/wwan0mbim0 --ms-query-slot-info-status=1"

# NetworkManager
nmcli device status
nmcli connection show

# Firmware update
fwupdmgr get-devices   # check modem shows up
fwupdmgr update        # update modem firmware if available
```

## References

- [lpac GitHub](https://github.com/estkme-group/lpac)
- [lpac MBIM driver source](https://github.com/estkme-group/lpac/blob/main/driver/apdu/mbim.c)
