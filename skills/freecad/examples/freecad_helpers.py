"""Shared helpers for FreeCAD scripts running inside the test environment.

Uses only stdlib + FreeCAD's bundled Python (no Bazel workspace imports).
Targets the pinned FreeCAD 1.2.dev environment (PySide6).
"""

import os
import sys
import time
import traceback

import FreeCAD

# gazelle:ignore PySide6,PySide6.QtCore
# (Qt ships inside the pinned FreeCAD environment; not a repo dependency)
from PySide6 import QtGui, QtWidgets
from PySide6.QtCore import QTimer

_t0 = time.monotonic()
_OSIFONT_FILENAME = "osifont-lgpl3fe.ttf"


def log(msg):
    """Print a timestamped message to stderr."""
    print(f"[{time.monotonic() - _t0:.3f}] {msg}", file=sys.stderr, flush=True)


def register_techdraw_fonts():
    """Register FreeCAD's bundled TechDraw font with Qt.

    TechDraw writes ``osifont`` into exported pages. Registering the font from
    FreeCAD's own resource tree keeps Qt from resolving that family through
    the runner's system-font fallback, which otherwise makes text metrics and
    PDF font embedding depend on the host image.
    """
    font_path = os.path.join(  # noqa: PTH118 — FreeCAD resource path
        FreeCAD.getResourceDir(), "Mod", "TechDraw", "Resources", "fonts", _OSIFONT_FILENAME
    )
    if not os.path.isfile(font_path):  # noqa: PTH113 — FreeCAD resource path
        raise FileNotFoundError(f"FreeCAD TechDraw font not found: {font_path}")

    font_id = QtGui.QFontDatabase.addApplicationFont(font_path)
    if font_id < 0:
        raise RuntimeError(f"Could not register FreeCAD TechDraw font: {font_path}")
    families = QtGui.QFontDatabase.applicationFontFamilies(font_id)
    log(f"Registered FreeCAD TechDraw font {font_path}: {list(families)}")


def init_gui():
    """Get the running QApplication. Call at module level in GUI binary scripts."""
    register_techdraw_fonts()
    return QtWidgets.QApplication.instance()


def pump(qapp, seconds=3):
    """Process Qt events for a fixed duration."""
    t0 = time.monotonic()
    for _ in range(int(seconds * 10)):
        if qapp:
            qapp.processEvents()
        time.sleep(0.1)
    log(f"pump({seconds}) done in {time.monotonic() - t0:.2f}s")


def wait_for_view(view, qapp, timeout=15.0, poll_interval=0.05):
    """Poll until TechDraw view has visible edges, processing Qt events."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if qapp:
            qapp.processEvents()
        edges = view.getVisibleEdges()
        if len(edges) > 0:
            elapsed = time.monotonic() - t0
            log(f"TechDraw view ready: {len(edges)} edges after {elapsed:.2f}s")
            return
        time.sleep(poll_interval)
    raise TimeoutError(f"TechDraw view not ready after {timeout}s")


def run_gui_script(qapp, fn):
    """Schedule fn to run after QApplication::exec() starts, always quitting exec() on completion.

    When using the FreeCAD GUI binary, all script work must be deferred via QTimer because
    exec() hasn't started yet when module-level code runs. If fn raises an exception,
    PySide would normally catch it, print it, and let exec() keep running indefinitely.
    This wrapper ensures qapp.quit() is always called so the process exits cleanly.

    Usage (replace bare QTimer.singleShot at end of script):
        run_gui_script(qapp, _work)
    """

    def _close_all_docs():
        # Close all open FreeCAD documents before quitting to prevent the
        # "save changes?" dialog that blocks qapp.quit() in GUI mode.
        # FreeCAD.closeDocument() forces close without prompting.
        try:
            for name in list(FreeCAD.listDocuments().keys()):
                FreeCAD.closeDocument(name)
        except Exception:
            pass

    def _wrapper():
        try:
            fn()
        except BaseException:
            traceback.print_exc(file=sys.stderr)
            # Quit the event loop, then force-exit: sys.exit() inside a Qt slot
            # is swallowed by PySide, so os._exit() is the only reliable escape.
            _close_all_docs()
            if qapp:
                qapp.quit()
            os._exit(1)
        else:
            _close_all_docs()
            if qapp:
                qapp.quit()

    QTimer.singleShot(0, _wrapper)
