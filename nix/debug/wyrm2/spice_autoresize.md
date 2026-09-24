# SPICE Dynamic Resize vs. monitors.xml (wyrm2)

**Gotcha**: SPICE auto-resize (guest resolution follows the remote-viewer
window) silently stops working the moment anything writes a
`~/.config/monitors.xml` entry for the virtual display — typically by
touching Settings → Displays in the guest. Delete the file to restore
auto-resize; the deletion only takes effect on the next GNOME session start
(Mutter keeps the explicit config in memory for the running session).

```bash
rm -f ~/.config/monitors.xml ~/.config/monitors.xml~   # then log out/in or reboot
```

## Mechanism

- Resizing the SPICE client window makes QEMU advertise the new window size
  as the virtual display's **preferred mode** (visible as `is-preferred` in
  `org.gnome.Mutter.DisplayConfig.GetCurrentState`, or as the first entry of
  `/sys/class/drm/card*-Virtual-1/modes`).
- Mutter follows the changing preferred mode **only when it has no stored
  configuration** for that monitor. A `monitors.xml` entry (or the same
  config still held in session memory) always wins, and there is no error or
  log anywhere — resize requests just stop being applied. `spice-vdagent`
  logs `Found monitor Virtual-1 with geometry …` only when a mode change
  actually lands, so silence there means "not applied".

## Flip side

If a **fixed** guest resolution is ever wanted (e.g. pinning 4K for a
Sunshine/Moonlight stream, see <gpu-strategy.md>), setting it in
Settings → Displays is exactly the right tool — just know it's mutually
exclusive with window-following SPICE until `monitors.xml` is deleted again.

Diagnosed 2026-07-02 on wyrm2 (GNOME 49 Wayland, `vga: virtio`).
