"""
Render a FreeCAD FCStd file to PNG with 3D perspective and lighting.

Runs under the FreeCAD GUI binary (not freecadcmd) — requires the GUI binary
so that QApplication::exec() runs, enabling a proper OpenGL viewport.
Reads INPUT env var for the FCStd path and OUTDIR for output directory.

Usage:
  Xvfb :99 -screen 0 1024x768x24 -nolisten tcp &
  DISPLAY=:99 INPUT=/work/model.FCStd OUTDIR=/output freecad /work/render_fcstd.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import FreeCAD as App
import FreeCADGui as Gui
from freecad_helpers import init_gui, log, pump, run_gui_script

input_path = os.environ.get("INPUT", "cube_with_hole.FCStd")
outdir = os.environ.get("OUTDIR", ".")

qapp = init_gui()


def _render() -> None:
    from pivy import coin  # noqa: PLC0415 — must import after exec() starts

    log("loading document")
    doc = App.openDocument(input_path)
    App.setActiveDocument(doc.Name)
    Gui.ActiveDocument = Gui.getDocument(doc.Name)

    log("pump for document load")
    pump(qapp, 2)

    log("configuring view properties")
    # Use exact TypeId match — many types (Sketcher::SketchObject, etc.) inherit from
    # Part::Feature but don't support Shaded display mode.
    for obj in doc.Objects:
        if obj.TypeId != "Part::Feature":
            continue
        vo = obj.ViewObject
        vo.Visibility = True
        vo.DisplayMode = "Shaded"
        vo.ShapeColor = (0.75, 0.75, 0.80)  # light blue-gray
        vo.Lighting = "One side"
        vo.Transparency = 0

    log("pump for view properties")
    pump(qapp, 2)

    view = Gui.ActiveDocument.ActiveView

    # Set white background
    param = App.ParamGet("User parameter:BaseApp/Preferences/View")
    param.SetBool("Gradient", False)
    param.SetUnsigned("BackgroundColor", 0xFFFFFFFF)

    # Use perspective projection for depth
    cam = view.getCameraNode()
    if cam.getTypeId().getName() == "SoOrthographicCamera":
        Gui.runCommand("Std_PerspectiveCamera", 0)
        pump(qapp, 1)
        cam = view.getCameraNode()

    cam.position.setValue(coin.SbVec3f(40, -30, 35))
    cam.pointAt(coin.SbVec3f(0, 0, 0))
    cam.nearDistance.setValue(1.0)
    cam.farDistance.setValue(200.0)

    # Add directional light for face contrast (default headlight follows the camera)
    root = view.getSceneGraph()
    light = coin.SoDirectionalLight()
    light.direction.setValue(coin.SbVec3f(-0.5, 0.3, -0.8))
    light.intensity.setValue(0.8)
    root.insertChild(light, 0)

    log("pump for camera + lighting")
    pump(qapp, 2)

    view.fitAll()
    log("pump for fitAll")
    pump(qapp, 1)

    output_name = Path(input_path).stem + ".png"
    output_path = os.path.join(outdir, output_name)  # noqa: PTH118 — FreeCAD API expects str
    view.saveImage(output_path, 800, 600, "Current")
    log(f"rendered: {output_path} — done")

    doc.setClosable(True)
    App.closeDocument(doc.Name)


# Defer work until after QApplication::exec() starts — the GUI binary enters
# exec() after processCmdLineFiles() returns, so module-level code here runs
# before the event loop is live. run_gui_script handles clean exit (qapp.quit()).
# See debug/qt_shutdown_segfault.md.
run_gui_script(qapp, _render)
