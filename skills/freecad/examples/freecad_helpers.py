"""Shared helpers for FreeCAD scripts running inside the test container.

Mounted alongside FreeCAD scripts as a plain file.
Uses only stdlib + FreeCAD's bundled Python (no Bazel workspace imports).
Targets FreeCAD 1.1.0+ (PySide6). For older FreeCAD (0.21, PySide2) you'd
need to swap the PySide6 imports below.
"""

import os
import sys
import time
import traceback

import FreeCAD
from PySide6 import QtWidgets
from PySide6.QtCore import QTimer

_t0 = time.monotonic()


def log(msg):
    """Print a timestamped message to stderr."""
    print(f"[{time.monotonic() - _t0:.3f}] {msg}", file=sys.stderr, flush=True)


def init_gui():
    """Get the running QApplication. Call at module level in GUI binary scripts."""
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
