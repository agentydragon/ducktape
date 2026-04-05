"""Export TechDraw SVG and PDF using the FreeCAD GUI binary.

Designed to be run as: FreeCAD /work/gui_export.py
(NOT freecadcmd — uses the GUI binary which calls QApplication::exec())

INPUT env var: path to bracket.FCStd
OUTDIR env var: output directory (default: /tmp/out)
"""

import os
import sys
import time
import traceback
from pathlib import Path

import FreeCAD as App
import FreeCADGui as Gui

sys.path.insert(0, "/work")  # freecad_helpers.py is mounted alongside

from freecad_helpers import log, pump

try:
    from PySide6 import QtCore, QtWidgets
except ImportError:
    from PySide2 import QtCore, QtWidgets

input_path = os.environ.get("INPUT", "/tmp/out/bracket.FCStd")
outdir = os.environ.get("OUTDIR", "/tmp/out")

log(f"gui_export.py starting: {input_path=}, {outdir=}")

qapp = QtWidgets.QApplication.instance()
log(f"QApplication instance: {qapp}")


def do_export():
    log("do_export: starting")
    try:
        _run_export()
    except Exception as e:
        log(f"ERROR in do_export: {e}")
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        if qapp:
            qapp.quit()
        else:
            os._exit(1)


def _run_export():
    import TechDrawGui  # noqa: PLC0415 — must import after GUI is initialized

    log(f"Opening document: {input_path}")
    doc = App.openDocument(input_path)
    log(f"Document opened: {doc.Name}, objects: {[o.Name for o in doc.Objects]}")

    pump(qapp, 1.0)

    pages = [o for o in doc.Objects if o.TypeId == "TechDraw::DrawPage"]
    if not pages:
        raise RuntimeError("No TechDraw::DrawPage found in document")
    page = pages[0]
    log(f"Found page: {page.Name}")

    views = [o for o in doc.Objects if o.TypeId == "TechDraw::DrawViewPart"]
    if views:
        view = views[0]
        log("Waiting for TechDraw HLR...")
        t0 = time.monotonic()
        while time.monotonic() - t0 < 20.0:
            if qapp:
                qapp.processEvents()
            try:
                edges = view.getVisibleEdges()
                if len(edges) > 0:
                    log(f"View ready: {len(edges)} edges after {time.monotonic() - t0:.2f}s")
                    break
            except Exception as e:
                log(f"getVisibleEdges error: {e}")
            time.sleep(0.05)
        else:
            log("WARNING: View not ready after 20s, proceeding anyway")

    pump(qapp, 0.5)

    svg_path = os.path.join(outdir, "bracket.svg")  # noqa: PTH118 — FreeCAD API expects str
    log(f"Exporting SVG: {svg_path}")
    TechDrawGui.exportPageAsSvg(page, svg_path)
    svg = Path(svg_path)
    if svg.exists():
        log(f"SVG exported: {svg.stat().st_size} bytes, header: {svg.read_bytes()[:80]}")
    else:
        log("ERROR: SVG file not created")

    pdf_path = os.path.join(outdir, "bracket.pdf")  # noqa: PTH118 — FreeCAD API expects str
    log(f"Exporting PDF: {pdf_path}")
    TechDrawGui.exportPageAsPdf(page, pdf_path)
    pdf = Path(pdf_path)
    if pdf.exists():
        log(f"PDF exported: {pdf.stat().st_size} bytes, header: {pdf.read_bytes()[:10]}")
    else:
        log("ERROR: PDF file not created")

    # Suppress the "unsaved changes" dialog. FreeCAD 1.1.0 has no doc.Modified
    # attribute; setClosable(True) bypasses the save dialog on closeDocument().
    doc.setClosable(True)
    App.closeDocument(doc.Name)
    log("Document closed")
    pump(qapp, 0.5)

    mw = Gui.getMainWindow()
    if mw:
        mw.close()
        log("Main window closed")
    elif qapp:
        qapp.quit()

    log("do_export complete")


# Schedule work via QTimer so it runs after the event loop starts.
QtCore.QTimer.singleShot(500, do_export)
log("QTimer.singleShot scheduled — waiting for event loop")
