# Retired workstation features

- **Tesseract:** No longer installed by the shared workstation module on Wyrm2,
  Rugged, or Iguana. Its full language data added about 1 GiB to Wyrm2's previous
  system closure. Install it explicitly if OCR is needed, preferably with only
  the required language data.
- **Sway:** The experimental Sway session and its Waybar, SwayNC, Foot and
  debug-session configuration were retired. GNOME remains the local desktop.
  Before restoring Sway, pin wlroots to the display GPU: it ignores the `mutter-device-ignore` rules that
  isolate Wyrm2's other two GPUs. See
  [the display investigation](../debug/wyrm2/direct_display_bringup/README.md).
- **XRDP and Xfce on Wyrm2:** The pre-login remote session is parked, along
  with Rugged's launcher and FreeRDP package. To restore it, uncomment both
  `rdpSession` and the XRDP/Xfce settings in `nix/nixos/hosts/wyrm2/default.nix`;
  restore Rugged's launcher and package in `nix/home/hosts/rugged.nix` if needed.
  Review the [remote desktop investigation](../debug/wyrm2/remote_desktop_wyrm2.md)
  and its Nebula/firewall access model before enabling the listener. GNOME Remote
  Login remains configured, but its system listener was not verified working;
  Sunshine requires a logged-in session.
- **ModemManager:** Disabled on Wyrm2 because the VM has no attached cellular
  modem. Re-enable `networking.modemmanager.enable` if a cellular modem is
  passed through. Rugged's WWAN configuration still enables it.

Nix store paths can remain reachable through old generations, other packages,
or other garbage-collection roots after a rebuild.
