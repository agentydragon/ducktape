# AirSense 11 Bluetooth

Verified on wyrm2 on 2026-10-29 with AirSense software
`SW04600.18.8.7.0.2d0405660` and
[airbreak-plus](https://github.com/m-kozlowski/airbreak-plus) commit
`0f14b51fe96f4fec987b5dcf33acbdd37e4bf92e`.
Pairing, authenticated reconnection, historical downloads, and a short idle
live stream succeeded. Therapy settings were not changed.

## Adapter

TP-Link UB500 (`2357:0604`), passed from atlas to wyrm2 VM 110 by device ID
(`usb5`, USB 2), declared in `cluster/terraform/main/proxmox-vms.tf`.
The dongle is connected through the Thunderbolt KVM at atlas USB path
`5-1.4.4`; passing the device by ID allows moving it between ports.

## Pairing

1. Temporarily turn off Bluetooth on the phone using myAir. Unpairing the phone
   was not necessary; coexistence after turning its Bluetooth back on is untested.
2. **Before starting the client**, open **More → myAir** on the AirSense and
   advance past the download/QR screen with **OK, downloaded**. Stay on that screen.
3. Run the client's pairing command and enter the displayed four-digit code.
   Do not reopen the machine's myAir menu during the exchange.

**Gotcha:** opening the pairing screen after the client starts resets the
machine's session state and invalidates the exchange. Even the displayed code
then returns `RPC error -11005: VerificationFailure`.
[Maintainer's explanation](https://github.com/m-kozlowski/airbreak-plus/issues/49#issuecomment-6158035431).

With an airbreak-plus checkout at `/tmp/airbreak-plus-ble`, the tested Nix environment is:

```bash
nix shell --impure --expr '
  let pkgs = (builtins.getFlake "github:NixOS/nixpkgs/6fcc160353f63555b560f25588925133c8f61f2a").legacyPackages.x86_64-linux;
  in pkgs.python3.withPackages (p: [p.bleak p.cryptography p.pyserial])
'
```

Inside that shell:

```bash
python /tmp/airbreak-plus-ble/python/as11_config.py devices pair 8C:6F:B9:05:48:5A
python /tmp/airbreak-plus-ble/python/as11_config.py -d ble:8C:6F:B9:05:48:5A rpc --method GetVersion
```

The client stores reusable pairing secrets in `~/.as11_ble.json`; keep that
file private and out of Git. Later connections use those credentials without
another on-screen code.

An encrypted copy lives at `secrets/cpap-ble.sops.json`, readable with the
workstation user SSH keys listed in `.sops.yaml` (plus admin recovery).
To restore on another machine, run from the repo root:

```bash
(
  umask 077
  export SOPS_AGE_KEY="$(ssh-to-age -private-key -i ~/.ssh/id_ed25519)"
  sops -d secrets/cpap-ble.sops.json > ~/.as11_ble.json
)
```

This replaces the local credential file; merge entries first if it already
contains other device pairings. The credentials are independent of the dongle;
using another adapter or simultaneous clients remains untested.

## SD card versus Bluetooth

Checked on 2026-10-29 against this machine's Bluetooth interface and the
`cpap-data/cpap-data` archive, including EDF headers from `DATALOG/20261028`.
Keep SD sync as the primary archive; Bluetooth complements it with live access
and diagnostics. Bluetooth returns structured records and samples, not the
original EDF files.

| Data                              | SD archive                                                                                          | Bluetooth evidence                                                                                                                |
| --------------------------------- | --------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Daily summaries                   | `STR.EDF`: 78 fields, including usage, AHI breakdown, pressure/leak statistics and settings         | `Summary` returned seven daily records; field-by-field equivalence unchecked                                                      |
| Historical flow and mask pressure | BRP: 25 Hz                                                                                          | `RespiratoryFlow6p25Hz` and `MaskPressure6p25Hz` returned samples at 160 ms intervals (6.25 Hz)                                   |
| Historical leak                   | PLD: 0.5 Hz                                                                                         | `Leak0p5Hz` returned samples at 2000 ms intervals (0.5 Hz)                                                                        |
| Other historical therapy metrics  | PLD: 0.5 Hz pressure, respiratory rate, tidal volume, minute ventilation, snore and flow limitation | `TherapyOneMinutePeriodic` returned 60 s samples; some decoded values were implausible, including negative respiratory rates      |
| Respiratory events                | EVE annotations                                                                                     | `TherapyEvents-RespiratoryEvents` returned central-apnea, obstructive-apnea and hypopnea events                                   |
| Settings                          | Settings fields in `STR.EDF`                                                                        | `SettingProfilesCollection` returned 14 historical snapshots in the first bounded download, with more available                   |
| Oximetry                          | SA2: 1 Hz pulse/SpO2 channels; channel presence does not prove valid measurements                   | Documented live pulse/SpO2 streams, not tested                                                                                    |
| Live flow and pressure            | BRP files recorded during therapy                                                                   | `StartStream` accepted both signals and delivered 40 ms samples during a three-second idle capture; therapy-long capture untested |
| Diagnostics                       | Therapy EDF files are not a general diagnostic export                                               | Additional error, cellular and machine-metric spools documented upstream, not tested                                              |

Morning Bluetooth downloads cannot recover the card's full 25 Hz flow/pressure
detail: archived samples are 6.25 Hz. Recording the live stream could preserve
25 Hz samples, but requires continuous capture and validation against the card.
Requesting faster sampling does not increase the native rate of low-rate signals.

Download probes used `--max-rounds 1` (two for `Summary`), so waveform and event
results demonstrate availability rather than complete-night coverage. The
configuration-profile probe restricted to the preceding two days returned no
records; that does not establish that configuration history is unsupported.

Before replacing SD collection, establish Bluetooth retention and complete-night
coverage, compare timestamps/scaling/events against EDFs, resolve the one-minute
decoder anomalies, and validate conversion for SD-oriented analysis tools.

References: [historical spools](https://github.com/m-kozlowski/airbreak-plus/blob/master/docs/as11/rpc_spools.md)
and [live stream mappings](https://github.com/m-kozlowski/airbreak-plus/blob/master/docs/as11/rpc_streams.md).
