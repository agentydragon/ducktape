# Root Cause: `Gui.showMainWindow()` Hangs on Fresh Container

## Symptom

`freecadcmd` with `QT_QPA_PLATFORM=offscreen` hangs indefinitely at or after:

```
Main window restored
Show main window
Toolbars restored
```

Script body never executes. Process must be killed.

## Root Cause: `DlgVersionMigrator::exec()`

The hang occurs **inside** `Gui.showMainWindow()`, not after it. The full call stack:

```
Gui.showMainWindow()
  setupMainWindow()
    StartupPostProcess::execute()
      checkVersionMigration()
        DlgVersionMigrator::exec()
          isCurrentVersionKnown()  → false on fresh container
          QDialog::exec()          ← BLOCKS: modal event loop waiting for button click
```

`isCurrentVersionKnown()` returns `false` when the FreeCAD versioned config directory
(e.g. `~/.local/share/FreeCAD/1.0`) does not exist. On a fresh container/RBE worker, it
doesn't exist. `QDialog::exec()` then starts a blocking modal event loop waiting for the
user to click a migration dialog — which is **invisible** on the offscreen platform.
Nobody clicks it. The process hangs forever.

This is why `showMainWindow()` never returns under `QT_QPA_PLATFORM=offscreen` on a
fresh environment: it blocks before it can return to the Python caller.

## Why xvfb-run Works

With `xvfb-run` providing a real X11 display (`xcb` platform), the dialog becomes
visible. Either:

- The RBE Docker image already has the versioned config directory (dialog is skipped), or
- Qt's event loop processes the dialog correctly on the real X11 display

Either way, `showMainWindow()` returns and the script continues normally.

## Fix (if hang recurs)

Pre-create the FreeCAD versioned config directory before invoking `freecadcmd`:

```bash
# Shell (adjust version to match the installed FreeCAD)
mkdir -p ~/.local/share/FreeCAD/1.0
```

Or from Python inside the script, before `Gui.showMainWindow()`:

```python
import pathlib
# Skip DlgVersionMigrator: pre-create versioned config dir so isCurrentVersionKnown() → True
pathlib.Path.home().joinpath(".local/share/FreeCAD/1.0").mkdir(parents=True, exist_ok=True)
```

Alternatively, set `FREECAD_USER_HOME` to a path with `usingCustomDirectories()` returning
`true`, which causes `DlgVersionMigrator::exec()` to return `0` immediately:

```bash
FREECAD_USER_HOME=/tmp/freecad-home xvfb-run -a freecadcmd script.py
```

## Current Solution

We use `xvfb-run -a freecadcmd` (see `conftest.py:freecad_headless`). This sidesteps the
offscreen hang. The root cause fix above is a fallback if xvfb is unavailable.

## References

- `src/Main/MainCmd.cpp` — `freecadcmd` entry point
- `src/Gui/FreeCADGuiPy.cpp:FreeCADGui_showMainWindow` — Python binding
- `src/Gui/StartupProcess.cpp:StartupPostProcess::execute()` — synchronous setup
- `src/Gui/Dialogs/DlgVersionMigrator.cpp` — the blocking dialog
