# FreeCAD Qt6 Shutdown Segfault

## Summary

All FreeCAD scripts that call `Gui.showMainWindow()` must end with `os._exit(0)` to
prevent a segfault during Qt6 cleanup. Scripts that never initialize the GUI (`FreeCADGui`
not imported) do not crash and do not need `os._exit(0)`.

## Stack Trace

Captured empirically from FreeCAD 1.1.0 under xvfb:

```
#0  /lib/x86_64-linux-gnu/libc.so.6(+0x45330)         — TLS access, already freed
#1  QThreadStorageData::get() const                    — reads OpenGL TLS slot
#2  QOpenGLContext::currentContext()                   — asks "is this surface current?"
#3  QSurface::~QSurface()
#4  QWindow::~QWindow()
#5  QWidgetPrivate::deleteTLSysExtra()
#6  QWidget::destroy(bool, bool)
#7  QWidget::~QWidget()
#8  libFreeCADGui.so(+0x2300d7d)                       — FreeCAD main window widget
#9  QObject::event(QEvent*)
#10 QFrame::event(QEvent*)
#11 QApplicationPrivate::notify_helper(QObject*, QEvent*)
#12 QCoreApplication::notifyInternal2(QObject*, QEvent*)
#13 QCoreApplicationPrivate::sendPostedEvents()        — flushing deferred widget deletes
#14-15 libQt6Core — thread cleanup hooks
#16-18 libc — thread TLS teardown
```

## Root Cause

Qt6 stores the current OpenGL context in thread-local storage via `QThreadStorageData`.
During process exit, libc tears down TLS as part of thread cleanup. Concurrently, Qt
processes a deferred `DeferredDelete` event for FreeCAD's main window. The widget
destructor calls `QSurface::~QSurface()`, which calls `QOpenGLContext::currentContext()`
to check whether this surface is the currently bound context — but by this point, the
TLS slot has already been freed. The result is a use-after-free → SIGSEGV.

The sequence that triggers it:

1. Script finishes; Python begins normal exit
2. Python invokes C extension cleanup and C++ static destructors
3. libc starts tearing down thread-local storage (TLS)
4. Qt's thread cleanup hooks fire `sendPostedEvents`, flushing the deferred-delete queue
5. FreeCAD's main window widget is destroyed; destructor accesses the now-freed OpenGL TLS

## Effect on Exit Code

FreeCAD installs a SIGSEGV handler that catches the crash, prints the backtrace, and
calls `exit()`. The exit code is not consistent:

| Script                    | GUI type             | Crash? | Exit code         | Test outcome                   |
| ------------------------- | -------------------- | ------ | ----------------- | ------------------------------ |
| `build_cube_with_hole.py` | None                 | No     | 0                 | Pass                           |
| `parametric_sketch.py`    | TechDraw             | Yes    | 0 (crash handler) | Pass despite crash             |
| `build_compound.py`       | TechDraw             | Yes    | 0 (crash handler) | Pass despite crash             |
| `export_page.py`          | TechDraw             | Yes    | 0 (crash handler) | Pass despite crash             |
| `render_fcstd.py`         | 3D viewport + OpenGL | Yes    | 1                 | **Fail** without `os._exit(0)` |

The 3D viewport case (`render_fcstd.py`) produces exit code 1 because the crash
involves a live OpenGL context that was actively rendering; the other scripts only
create OpenGL infrastructure for TechDraw's offscreen HLR renderer, which leaves the
context in a less entangled state when the crash fires.

## Why There Is No Clean Fix

The obvious alternatives don't work:

- **`sys.exit(0)`**: runs Python atexit and `__del__`, then triggers C extension and C++
  static destructor cleanup — the same path that crashes.
- **`qapp.exit(0)` + `qapp.exec()`**: requires running inside an event loop; `freecadcmd`
  scripts are not inside `exec()`.
- **`del qapp`**: the `QApplication` is owned by FreeCAD's C++ side; Python holds only a
  reference. Deleting the Python reference does not destroy the underlying C++ object, and
  even if it did, the same destructor sequence would fire.
- **`qapp.processEvents()` before exit**: drains already-posted events but cannot prevent
  the crash, because the offending `sendPostedEvents` call is triggered by libc's TLS
  teardown hooks — which run _after_ `sys.exit()` has already started unloading Python.

This is a bug in FreeCAD's interaction with Qt6's shutdown sequencing. It is not
fixable from Python scripts. `os._exit(0)` is the correct workaround: it calls the
OS-level `_exit()` syscall directly, bypassing all Python and C++ cleanup entirely.
All file I/O is complete before `os._exit(0)` is called, so no data is lost.
