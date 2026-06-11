# GNOME OSK Window Avoidance Report

Date: 2026-05-19
Host context: `rugged` / GNOME Wayland tablet use

## Question

GNOME's built-in on-screen keyboard (OSK) appears over applications. This is
especially bad when the focused text field is in the lower half of the screen.
The desired behavior is Android-like: when the OSK appears, the focused window
or content should move or resize so the text cursor remains visible.

## Short Answer

For stock GNOME Shell on Wayland, I did not find a supported setting or current
drop-in extension that makes windows or application content resize out of the
way of the built-in OSK.

There is one real Linux feature in this family, but it is mostly the old X11
model: Onboard's docked mode has a `docking-shrink-workarea` setting whose
schema says "Shrink workarea when docked" and "shrink the available space for
maximized windows." That is the actual "make windows avoid the keyboard" shape,
implemented through the X11 window-manager workarea/strut model.

That does not give us the same thing in stock GNOME Shell Wayland.

Linux has pieces of this elsewhere:

- Mobile shells such as Phosh are built around a compositor + OSK stack
  (`phoc` + `squeekboard`/`phosh-osk-stub`) and are a better fit for phone-like
  text input.
- App/toolkit-specific stacks such as Qt Virtual Keyboard can resize or pan app
  content, but that requires application participation.
- X11-era OSKs such as Onboard can be docked/moved and may reserve screen
  space under X11, but this is not a clean modern GNOME Wayland solution.
- GNOME extensions such as GJS OSK or Improved OSK improve the keyboard itself
  or let it float, but they do not appear to provide general Android-style
  window/content avoidance for the built-in GNOME OSK.

The practical conclusion for `rugged`: try a movable overlay OSK as a near-term
workaround, or build a GNOME Shell extension / Mutter patch if we want this
behavior while staying on GNOME Shell.

## GNOME State

GNOME design notes explicitly say that when the OSK is displayed, "the view
should be shifted in order to keep the text cursor visible." That is the desired
behavior, not a weird request:

- GNOME design wiki, screen keyboard behavior:
  <https://wiki.gnome.org/Design/OS/ScreenKeyboard>

Older GNOME bug discussion also described the same direction: a keyboard at the
bottom that sets struts and compresses the content area, or pans the workspace
when the keyboard is transient:

- GNOME Bugzilla 612662, original OSK support discussion:
  <https://bugzilla.gnome.org/show_bug.cgi?id=612662>

But current GNOME Shell layout code still treats the top panel differently from
the OSK. The panel is added as chrome with `affectsStruts: true`, while
`keyboardBox` is added with `addTopChrome(this.keyboardBox)` and no strut
parameter. In GNOME Shell's own comments, `affectsStruts` is what makes an edge
actor affect window-manager struts.

- GNOME Shell `layout.js`, `panelBox` vs `keyboardBox`:
  <https://github.com/GNOME/gnome-shell/blob/main/js/ui/layout.js>

This matches the observed behavior: the OSK is a shell overlay at the bottom of
the monitor, not a reserved work area that causes maximized or lower-positioned
windows to shrink.

GNOME users have reported the same UX failure for years. In one bug, a user
explicitly compares GNOME unfavorably to Android because GNOME lacks the "move
the input field upward" behavior:

- GNOME Bugzilla 742246:
  <https://bugzilla.gnome.org/show_bug.cgi?id=742246>

A 2024 GNOME Discourse thread also reports that screen contents do not move to
show the active field, and a GNOME developer notes that OSK height is limited in
GNOME Shell code:

- GNOME Discourse, GNOME 46 OSK discussion:
  <https://discourse.gnome.org/t/on-screen-keyboard-poor-alignment-at-400-and-often-closes-apps/20187>

## Wayland Protocol Boundary

The relevant Wayland protocol support is not the same thing as Android-style
window resize.

`text-input-unstable-v3` lets the application tell the compositor where the text
cursor is. Its `set_cursor_rectangle` request is for placing word suggestions
near the cursor without obstructing typed text. It does not, by itself, tell
applications the virtual keyboard geometry so they can resize their content.

- Wayland `text-input-unstable-v3`, `set_cursor_rectangle`:
  <https://cgit.freedesktop.org/wayland/wayland-protocols/tree/unstable/text-input/text-input-unstable-v3.xml>

`input-method-unstable-v2` includes input popup surfaces and a
`text_input_rectangle` hint, but again this is about input method/popup
placement around active text, not a universal "keyboard appeared, app viewport
is now smaller" contract:

- Wayland `input-method-unstable-v2`:
  <https://wayland.app/protocols/input-method-unstable-v2>

KDE's 2025 Plasma input-method sprint notes the same protocol gap directly:
TextInputV2 clients could be told where the virtual keyboard is; TextInputV3
does not. They list "should we move or resize?" as still-open design work.

- KDE Plasma 2025 virtual keyboard notes:
  <https://community.kde.org/Sprints/Plasma/2025/Topics/Input_methods_and_virtual_keyboards>

So the missing piece is not merely "install a different keyboard"; the compositor,
toolkit, and app need an agreed geometry/viewport behavior.

## Things That Exist

### Phosh / Squeekboard / Phoc

Phosh's keyboard stack is the closest Linux analogue to a mobile OSK flow.
Squeekboard is described as the OSK input method for Phosh, designed for
smartphones, tablet PCs, and touch devices:

- Squeekboard docs:
  <https://world.pages.gitlab.gnome.org/Phosh/squeekboard/>

Phosh also has `phosh-osk-stub`, which uses `text-input-unstable-v3` when apps
support it and falls back to `virtual-keyboard` behavior otherwise:

- Phosh OSK completion post:
  <https://phosh.mobi/posts/osk-completion/>

Phoc, the compositor normally used with Phosh, has an output `usable_area` and
layer-surface plumbing. That is the kind of compositor architecture where a
keyboard can naturally change the usable region:

- Phoc `Output` docs:
  <https://world.pages.gitlab.gnome.org/Phosh/phoc/class.Output.html>

Tradeoff: Phosh is a phone/mobile shell. It may be a better tablet-mode session
than GNOME Shell, but it is not a small GNOME preference flip.

### GJS OSK

GJS OSK is a GNOME Shell extension that provides a separate on-screen keyboard
implemented in GNOME JavaScript. As of this check, the GNOME Extensions page has
active builds for Shell `45` through `50`, so it is more current than the older
Improved OSK extension for GNOME 49-era systems:

- GJS OSK extension:
  <https://extensions.gnome.org/extension/5949/gjs-osk/>

Third-party writeups describe it as movable rather than stuck to the bottom. In
this report, that means a movable overlay: the keyboard is above other windows in
the stacking/compositor layer, but it does not change their geometry or reserve a
work area. That can avoid the lower-screen text-field problem only by letting the
user place the keyboard elsewhere; it is not automatic window avoidance:

- UbuntuHandbook GJS OSK overview:
  <https://ubuntuhandbook.org/index.php/2023/05/gjs-osk-more-usable-on-screen-keyboard/>

### Improved OSK

Improved OSK adds keys and size controls to GNOME's OSK. Its GNOME Extensions
page currently shows active versions only for Shell `43` and `44`, which matches
the existing `rugged` TODO that it is not ready for GNOME 49:

- Improved OSK extension:
  <https://extensions.gnome.org/extension/4413/improved-osk/>

It improves the keyboard layout; I did not find evidence that it makes app
windows avoid the OSK.

### Vboard

Vboard is a newer Wayland-oriented virtual keyboard with a `uinput` backend. It
explicitly calls out the gap that GNOME and KDE built-in keyboards lack keys
needed for desktop use, and says Onboard does not work on Wayland:

- Vboard:
  <https://archisman-panigrahi.github.io/vboard/>

This is promising as an accessibility/desktop keyboard alternative, but it is
not evidence of automatic GNOME window resize. It is more likely a better
movable-overlay keyboard workaround.

### Onboard

Onboard remains a good X11-era keyboard: movable, dockable, configurable, and
with fuller desktop keys. Multiple current references still point to Wayland as
the problem boundary. If we are willing to use an X11 session, it may provide the
old "dock and reserve space" behavior better than GNOME's built-in Wayland OSK.

This is the clearest found example of something that actually changes window
avoidance rather than merely drawing above windows. Its schema includes
`docking-shrink-workarea`, summarized as "Shrink workarea when docked", with the
description "When docked, shrink the avaliable space for maximized windows." In
X11 terms, this corresponds to the window-manager workarea model, where dock and
panel windows can reserve space via `_NET_WM_STRUT` / `_NET_WM_STRUT_PARTIAL` and
the window manager calculates `_NET_WORKAREA` from that.

Sources:

- Onboard GSettings schema:
  <https://sources.debian.org/src/onboard/1.4.1-5/data/org.onboard.gschema.xml/>
- EWMH workarea/strut spec:
  <https://xdg-specs-technobaboo-f55ac9d85e73073a0c8831695ba0fb110849811c0.pages.freedesktop.org/wm-spec/wm-spec-latest.html>
- User reports still point to X11 as the practical boundary for Onboard on
  modern Ubuntu/GNOME:
  <https://askubuntu.com/questions/1467401/onboard-screenkeyboard-does-not-work-in-ubuntu-22-04>

Tradeoff: switching `rugged` back to X11 would give up Wayland-native behavior
we already rely on for the modern GNOME/tablet stack.

## What Actually Moves Or Reserves Space?

Confirmed or high-confidence:

1. Onboard on X11, docked with workarea shrink enabled.
   This can reserve screen space for maximized windows. It is the strongest
   existing match, but it is X11-era and not a clean GNOME Wayland answer.

2. App-specific UI adaptation.
   Qt Virtual Keyboard can be integrated inside a Qt app and the app can resize
   or pan its own content around `Qt.inputMethod.keyboardRectangle`. This solves
   a Qt/embedded app, not arbitrary desktop windows.

3. KDE Plasma Wayland / KWin.
   Plasma appears better than GNOME for the narrow geometry question: current
   reports against Plasma 6 say opening the virtual keyboard "tiles with the
   other windows" and reduces their height. That is real compositor-level
   geometry movement, not just an overlay. The downside is that the same reports
   are about restore bugs, and KDE's own 2025 input-method notes still list
   "should we move or resize?" and protocol gaps as open design work.

4. Mobile shell/compositor stacks such as Phosh/phoc.
   These are architecturally closer to what we want, because the OSK is part of
   the mobile session/compositor contract. This should be tested as a separate
   tablet-mode session rather than assumed as a drop-in GNOME Shell fix.

Not confirmed:

- GJS OSK, Vboard, and Improved OSK as GNOME Wayland solutions that resize or
  move other app windows. They are keyboard replacements/overlays, not general
  window avoidance systems.
- KDE Plasma desktop as a polished solved answer. It is probably better than
  GNOME for compositor-side avoidance, but the virtual keyboard stack still has
  activation, input-method, non-KDE-app, and geometry-restore rough edges.

## KDE / Plasma Assessment

KDE is probably better than GNOME if the specific criterion is "does the window
manager ever move or resize windows when the virtual keyboard appears?"

Evidence:

- Plasma Keyboard is a Qt Virtual Keyboard based project designed to integrate
  with Plasma. Its README says it uses the `input-method-v1` Wayland protocol to
  communicate with the compositor, and KWin shows it when a text field is
  touched:
  <https://github.com/KDE/plasma-keyboard>
- A recent KWin virtual-keyboard bug report says that when the virtual keyboard
  opens, it "tiles with the other windows on the screen effectively reducing
  their height." That report says the behavior happens with both Maliit Keyboard
  and `plasma-keyboard`, which points at KWin/compositor behavior rather than a
  single keyboard app:
  <https://www.mail-archive.com/kde-bugs-dist%40kde.org/msg1116381.html>
- A matching KDE Discuss thread asks how to make KWin overlay the virtual
  keyboard instead of resizing windows, again implying that current Plasma can
  do the avoiding behavior, but not always restore geometry correctly:
  <https://discuss.kde.org/t/kwin-doesnt-restore-inactive-windows-after-virtual-keyboard-closes/41797>
- KDE's 2025 Plasma input-method sprint notes still call out unresolved design:
  TextInputV2 clients could be told where the virtual keyboard is, TextInputV3
  cannot, and KDE still has open questions around whether to move or resize:
  <https://community.kde.org/Sprints/Plasma/2025/Topics/Input_methods_and_virtual_keyboards>

Interpretation:

- KDE/Plasma Wayland is more promising than GNOME Shell for this particular
  feature because KWin has a compositor path that can change window geometry
  around the virtual keyboard.
- It is not yet an Android-quality answer. Expect rough edges around:
  - restoring tiled/side-by-side windows after the keyboard hides
  - whether the keyboard appears for all app/toolkit combinations
  - Electron/Chrome/non-KDE app input behavior
  - keyboard layout completeness, arrows, Tab, terminal use
- This should be tested as a parallel session on `rugged`, not adopted on the
  assumption that it is fixed.

Best next KDE experiment:

1. Add a Plasma 6 Wayland session on `rugged` without removing GNOME.
2. Enable/install `plasma-keyboard` and, if needed, Maliit for comparison.
3. Test the same bottom-half text field cases in:
   - a KDE/Qt app
   - Firefox or Chrome
   - Electron
   - a terminal
4. Specifically record:
   - whether windows shrink/move when the OSK appears
   - whether maximized and side-by-side/tiled windows restore correctly
   - whether the keyboard appears from touch focus in non-KDE apps

## Rugged KDE Probe

Current local config state:

- `nix/nixos/hosts/rugged/default.nix` now enables Plasma 6 as a parallel
  session while keeping GDM and default GNOME login intact:
  `services.desktopManager.plasma6.enable = true` and
  `services.displayManager.defaultSession = "gnome"`.
- The same host config includes `kdePackages.plasma-keyboard`,
  `maliit-framework`, and `maliit-keyboard` in `environment.systemPackages`.
- `nix/nixos/modules/gui.nix` sets the GNOME GSConnect package through
  `lib.mkDefault` so Plasma can use its KDE Connect package without an option
  conflict.
- The `rugged` toplevel built successfully with:
  `nix build .#nixosConfigurations.rugged.config.system.build.toplevel --no-link`.

Observed after switching the host:

- The Plasma/KWin virtual keyboard selector can now see both Plasma Keyboard and
  Maliit.
- Provider desktop files are installed in the system profile:
  - `/run/current-system/sw/share/applications/org.kde.plasma.keyboard.desktop`
    with `Exec=plasma-keyboard`
  - `/run/current-system/sw/share/applications/com.github.maliit.keyboard.desktop`
    with `Exec=maliit-keyboard`
  - both advertise `X-KDE-Wayland-VirtualKeyboard=true`
- `plasma-keyboard`, `maliit-keyboard`, `kwin_wayland`, `startplasma-wayland`,
  KWrite, Kate, and Konsole are available in `/run/current-system/sw/bin`.

Known weirdness:

- Opening the regular keyboard settings module can print:

  ```text
  kf.kcmutils: Error loading QML file qrc:/kcm/kcm_keyboard/main.qml
  qrc:/kcm/kcm_keyboard/main.qml:30:22: KCMKeyboard.KeyboardModel is not a type
  ```

  This is the normal keyboard-layout KCM (`kcm_keyboard`), not the virtual
  keyboard provider selector. The `KCMKeyboard.KeyboardModel` QML type and
  plugin files are present in the `plasma-desktop` store path, and `ldd` did not
  show missing shared-library dependencies for the declarative plugin. Treat
  this as a KDE/KCM runtime oddity unless it blocks layout configuration.

Nested Plasma probe:

- A helper script exists at `debug/rugged/osk_window_avoidance/start_nested_plasma.sh`.
- It starts nested KWin/Plasma from the current GNOME Wayland session with:
  - stable inner socket name: `nested-plasma-osk`
  - `--inputmethod /run/current-system/sw/bin/plasma-keyboard` by default
  - `KWIN_IM_SHOW_ALWAYS=1` by default
  - KWrite launched inside the nested session as a text-input target
- Run:

  ```bash
  debug/rugged/osk_window_avoidance/start_nested_plasma.sh
  ```

- Test Maliit instead with:

  ```bash
  NESTED_PLASMA_INPUTMETHOD=/run/current-system/sw/bin/maliit-keyboard \
    debug/rugged/osk_window_avoidance/start_nested_plasma.sh
  ```

- Disable the automatic KWrite launch, if needed, with:

  ```bash
  NESTED_PLASMA_TEST_APP= debug/rugged/osk_window_avoidance/start_nested_plasma.sh
  ```

How to make the OSK appear during the nested test:

1. Click or tap inside the KWrite document area in the nested Plasma window.
2. If auto-show does not fire, use Plasma's virtual-keyboard panel/tray applet.
   Plasma's own applet toggles the KWin virtual keyboard state and calls
   `forceActivate()` for clients that do not report text-input support.
3. From a terminal inside the nested Plasma session, force the KWin path
   directly:

   ```bash
   qdbus org.kde.KWin /VirtualKeyboard org.freedesktop.DBus.Properties.Set org.kde.kwin.VirtualKeyboard enabled true
   qdbus org.kde.KWin /VirtualKeyboard org.kde.kwin.VirtualKeyboard.forceActivate
   ```

Why `KWIN_IM_SHOW_ALWAYS=1` is in the helper:

- Plasma 6 currently gates virtual-keyboard show behavior in non-touch/tablet
  contexts. KDE Discuss notes that `KWIN_IM_SHOW_ALWAYS=1` is needed for the
  keyboard to show in those cases:
  <https://discuss.kde.org/t/virtual-keyboard-doesn-t-work-on-lock-screen/41542>
- KWin exposes the virtual keyboard DBus object at `/VirtualKeyboard`, with
  `enabled`, `active`, `visible`, `available`, `willShowOnActive()`, and
  `forceActivate()`:
  <https://sources.debian.org/src/kwin/4%3A6.3.6-1/src/virtualkeyboard_dbus.cpp/>

What still needs manual observation:

- Whether nested KWin actually resizes/moves KWrite when the OSK appears.
- Whether the behavior differs between Plasma Keyboard and Maliit.
- Whether the same resize/restore behavior holds in a real Plasma login session,
  which is a stronger test than nested KWin inside GNOME.
- Whether Firefox/Chrome/Electron text fields trigger the OSK and preserve the
  focused cursor area.

## Practical Options For Rugged

1. Test KDE Plasma Wayland as a parallel tablet session.
   It is the most promising desktop-shell candidate found for actual
   compositor-side window avoidance.

2. Try GJS OSK inside GNOME.
   It is current for GNOME 49/50 and likely the least invasive GNOME workaround.
   The expected win is "move the keyboard somewhere less destructive," not true
   Android-style window resize.

3. Try Vboard if GJS OSK is not good enough.
   It is designed for modern Wayland desktops and has fuller desktop keys. It may
   require `uinput` permissions and Nix packaging work.

4. Test a Phosh session as a tablet-mode experiment.
   This is the most plausible ready-made Linux stack for mobile-style OSK
   behavior, but it changes the shell model. It should be evaluated as "tablet
   mode session for Rugged," not as a GNOME Shell extension.

5. Build a custom GNOME Shell extension.
   A local extension could listen for OSK visibility/height and move or resize
   the focused `Meta.Window` when it intersects the keyboard rectangle. This
   would be a compositor-side workaround. It would need careful handling for
   maximized windows, tiled windows, multi-monitor layouts, browser/Electron
   apps, and restoring geometry when the keyboard hides.

6. Patch GNOME Shell / Mutter.
   The cleanest GNOME-native approach would be upstream-shaped: implement dynamic
   OSK avoidance in Shell/Mutter, probably using cursor rectangles when available
   and falling back to focused-window geometry. Merely adding
   `affectsStruts: true` to `keyboardBox` is unlikely to be sufficient because
   GNOME's own `affectsStruts` comment says visibility changes do not control
   whether a strut exists.

## Suggested Next Test

For a low-cost experiment:

1. Package or install GJS OSK on `rugged`.
2. Disable the built-in Screen Keyboard to avoid conflicts.
3. In a GNOME Wayland session, test:
   - GNOME Text Editor with a field near the bottom.
   - Chrome/Electron under native Wayland (`NIXOS_OZONE_WL=1` is already set).
   - Password fields and login/lock-screen limitations.
4. Record whether a movable overlay OSK is enough, or whether we need actual
   compositor-level window avoidance.

If movable is not enough, the next real implementation step is a small local
GNOME Shell extension prototype that moves/restores the focused window based on
the visible keyboard rectangle.
