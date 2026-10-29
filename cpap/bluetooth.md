# AirSense 11 Bluetooth

Verified on wyrm2 on 2026-10-29 with AirSense software
`SW04600.18.8.7.0.2d0405660` and
[airbreak-plus](https://github.com/m-kozlowski/airbreak-plus) commit
`0f14b51fe96f4fec987b5dcf33acbdd37e4bf92e`.
Pairing and a subsequent authenticated `GetVersion` query succeeded; therapy
settings were not changed. Historical downloads and streaming remain untested.

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
