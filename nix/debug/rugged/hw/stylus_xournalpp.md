# Xournal++ passive stylus input

**Status**: The passive stylus is plain capacitive touchscreen input on rugged.
Xournal++ probably needs to treat touchscreen input as drawing input for this
stylus to ink. Live test in progress; not yet made durable in Nix/Home Manager.

**Hardware**: Dell Pro Rugged 12 Tablet RA02260, EETI touchscreen controller
`EETI8082:00 0EEF:C005`.

## 2026-06-14 investigation

Original symptom: in Xournal++ 1.3.2 on GNOME/Wayland, using the passive stylus
with the brush panned the page instead of drawing.

Relevant Xournal++ setting before the fix:

```xml
<property name="touchDrawing" value="false"/>
```

Kernel input devices seen for the EETI controller:

| Device               | Kernel name                    | udev classification      | Experiment result                          |
| -------------------- | ------------------------------ | ------------------------ | ------------------------------------------ |
| `/dev/input/event11` | `EETI8082:00 0EEF:C005`        | `ID_INPUT_TOUCHSCREEN=1` | Finger and passive stylus both emit events |
| `/dev/input/event12` | `EETI8082:00 0EEF:C005 Mouse`  | `ID_INPUT_MOUSE=1`       | Silent for finger and passive stylus       |
| `/dev/input/event13` | `EETI8082:00 0EEF:C005 Stylus` | `ID_INPUT_TABLET=1`      | Silent for finger and passive stylus       |

`evtest` on `/dev/input/event11` for both finger and passive stylus showed the
same event family:

```text
BTN_TOUCH
ABS_MT_TRACKING_ID
ABS_MT_POSITION_X
ABS_MT_POSITION_Y
ABS_X
ABS_Y
MSC_TIMESTAMP
```

There were no stylus-specific or pressure events in the passive stylus capture:
no `BTN_TOOL_PEN`, `BTN_STYLUS`, `ABS_PRESSURE`, `ABS_MT_PRESSURE`, or
`ABS_MT_TOOL_TYPE`. So the passive stylus is not distinguishable from a finger at
the kernel event layer. The separate tablet/stylus node exists, but this passive
stylus does not drive it.

## Xournal++ behavior

Source checked against upstream `xournalpp` tag `v1.3.2`.

`InputContext` routes touchscreen events to the panning/touch handler unless
`Settings::getTouchDrawingEnabled()` is true. With touch drawing enabled,
Xournal++ first gives touchscreen events to `TouchDrawingInputHandler` and then
falls through to the normal touch handler for multitouch sequences.

This means:

- Single contact from either finger or the passive stylus draws with the current
  drawing tool.
- Two-finger gestures should still be available for panning/zooming.
- Xournal++ cannot make only the passive stylus draw while preserving
  single-finger panning, because both arrive as the same touchscreen events.

## Current experiment

Live user config was updated:

```xml
<property name="touchDrawing" value="true"/>
```

Restart Xournal++ after changing the setting; it reads `settings.xml` at
startup.

TODO after testing:

- If `touchDrawing=true` is acceptable in daily use, make it durable in the
  rugged Home Manager config.
- Decide whether to own just this setting via an activation patch or to manage a
  minimal Xournal++ settings file fragment another way. Avoid committing the
  whole mutable `settings.xml`.

Already durable:

- `nix/nixos/hosts/rugged/default.nix` includes `evtest` in
  `environment.systemPackages` for future input-device debugging.

## Active pen / Linux report search

Question: Dell sells active pens with barrel buttons and eraser affordances. Are
there Linux reports that make buying one a good bet for the Dell Pro Rugged 12?

Summary as of 2026-06-14: no exact Linux report found for this tablet or this
digitizer, but Dell active pens have adjacent Linux support reports on other
Dell/ELAN/Wacom AES devices.

Exact-model searches:

- No useful public Linux report found for `RA02260`, `Dell Pro Rugged 12`,
  `EETI8082:00 0EEF:C005`, or `0EEF:C005`.
- linux-hardware has zero entries for the input device:
  <https://linux-hardware.org/?view=search&vendorid=0eef&deviceid=c005>
- linux-hardware has no submitted probes for `Dell Pro Rugged 12`:
  <https://linux-hardware.org/?view=computers&vendor=Dell&model=Pro%20Rugged%2012>
- linux-hardware does have a nearby `Dell Pro Rugged 14 RB14250` entry, but that
  is a notebook with different hardware and should not be used as evidence for
  this tablet.
- Upstream `systemd` hwdb searches did not show an `EETI` / `0EEF:C005` /
  Rugged stylus quirk.

Dell official docs:

- The RA02260 service manual describes the stored/tethered "stylus", which
  appears to be the passive stylus tested here. I did not find RA02260-specific
  active-pen behavior in Dell's manual pages.
- Dell PN557W documentation says the bottom barrel button is erase by default,
  the top barrel button is right-click/context menu by default, and the top
  Bluetooth button is for Windows actions such as OneNote/screenshot/wake:
  <https://www.dell.com/support/manuals/en-us/dell-active-pen-pn557w/dell_pn557w_pen_ug/features?guid=guid-50720a50-de56-4c78-a214-cf84e7a85ad0&lang=en-us>
- The Windows Active Pen Control Panel can map barrel buttons to erase,
  right-click, page up/down, copy/paste, undo, or redo, and can enable barrel
  buttons while hovering:
  <https://www.dell.com/support/manuals/en-us/dell-active-pen-pn557w/dell_pn557w_pen_ug/using-active-pen-control-panel?guid=guid-9affefd5-a793-489e-9896-29761003dd02&lang=en-us>

Adjacent Linux evidence:

- `libwacom`'s `wacom.stylus` database contains Dell Active Pen entries,
  including comments for `PN557W`, `PN5122W`, `PN7320A`, and `PN579X`.
- `linuxwacom/libwacom#933` reports Dell Pro Premium Active Pen `PN7522W` as an
  AES pen with three buttons, tilt, and pressure, "fully workable" on an
  `ELAN3233:00 04F3:42AB Stylus` device:
  <https://github.com/linuxwacom/libwacom/issues/933>
- `flxzt/rnote#209` reports `Dell Premium Active Pen PN579X` on Arch/Sway. In
  `libinput debug-events`, one barrel button appears as `BTN_STYLUS`; the other
  switches the tool between `pen` and `eraser` proximity events:
  <https://github.com/flxzt/rnote/issues/209>
- `linuxwacom/input-wacom#288` is a cautionary report: `Dell Active Pen PN579X`
  on an XPS 9310 2-in-1 regressed under Linux 5.15.6 after working under 5.15.5:
  <https://github.com/linuxwacom/input-wacom/issues/288>

Interpretation:

- A Dell active pen can plausibly expose pressure plus barrel-button/eraser
  events under Linux, but there is no public proof for this Rugged 12
  `EETI8082:00 0EEF:C005` controller.
- If buying, prefer a returnable/borrowed active pen and treat it as an
  experiment. The key question is whether active-pen events appear on
  `/dev/input/event13` or another tablet-class event node; the passive stylus did
  not drive that node.

Active pen test checklist:

```bash
sudo libinput list-devices
sudo libinput debug-events --show-keycodes
sudo evtest /dev/input/event13
sudo evtest
```

While testing the active pen, look for:

- `BTN_TOOL_PEN`
- `ABS_PRESSURE`
- `BTN_STYLUS`
- `BTN_STYLUS2`
- `BTN_TOOL_RUBBER`
- libinput `TABLET_TOOL_BUTTON`
- libinput `pen` / `eraser` proximity transitions

If those show up, Xournal++/Rnote should have a much better chance of mapping
buttons or eraser behavior than with the passive stylus.

## Reproduction commands

Use `evtest` from the rugged system package set, or temporarily via Nix before
the NixOS generation containing `evtest` is active:

```bash
nix shell nixpkgs#evtest -c sh -lc 'sudo "$(command -v evtest)" /dev/input/event11'
```

Then draw/touch anywhere on the built-in screen. `evtest` is terminal-only; it
does not create a separate window and is not focus-bound.

For a full device picker:

```bash
sudo evtest
```

Expected result for the passive stylus on this hardware: events appear on
`event11`, not `event13`.
