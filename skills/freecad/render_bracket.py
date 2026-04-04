"""
Render bracket FCStd to PNG from two camera angles (front-right and rear-left).

Runs inside freecadcmd under xvfb (needs Qt/OpenGL for 3D viewport rendering).
Reads INPUT env var for the FCStd path and OUTDIR for output directory.
Outputs: bracket_front.png, bracket_rear.png

Usage:
  INPUT=/work/bracket.FCStd OUTDIR=/output \
    xvfb-run -a -s "-screen 0 1024x768x24" freecadcmd render_bracket.py
"""

import os
import time

import FreeCAD as App
import FreeCADGui as Gui

Gui.showMainWindow()

try:
    from PySide6 import QtWidgets
except ImportError:
    from PySide2 import QtWidgets

qapp = QtWidgets.QApplication.instance()


def pump(seconds=3):
    """Process Qt events to let FreeCAD's background computation run."""
    for _ in range(int(seconds * 10)):
        if qapp:
            qapp.processEvents()
        time.sleep(0.1)


input_path = os.environ.get("INPUT", "bracket.FCStd")
outdir = os.environ.get("OUTDIR", ".")

# === Load document ===
doc = App.openDocument(input_path)
App.setActiveDocument(doc.Name)
Gui.ActiveDocument = Gui.getDocument(doc.Name)

pump(2)

# Configure view properties for shaded rendering.
# Only set DisplayMode on objects that support "Shaded" (solid bodies, not sketches).
for obj in doc.Objects:
    if not obj.isDerivedFrom("Part::Feature"):
        continue
    vo = obj.ViewObject
    if "Shaded" not in vo.listDisplayModes():
        vo.Visibility = False
        continue
    vo.Visibility = True
    vo.DisplayMode = "Shaded"
    vo.ShapeColor = (0.55, 0.60, 0.70)  # Steel blue-gray, distinct from white background
    vo.Lighting = "One side"
    vo.Transparency = 0

pump(2)

# === Common view setup ===
param = App.ParamGet("User parameter:BaseApp/Preferences/View")
param.SetBool("Gradient", False)
param.SetUnsigned("BackgroundColor", 0xFFFFFFFF)

from pivy import coin  # noqa: E402 — must import after Gui init

view = Gui.ActiveDocument.ActiveView

# Switch to perspective if needed
cam = view.getCameraNode()
if cam.getTypeId().getName() == "SoOrthographicCamera":
    Gui.runCommand("Std_PerspectiveCamera", 0)
    pump(1)
    cam = view.getCameraNode()

# Add directional light for face contrast
root = view.getSceneGraph()
light = coin.SoDirectionalLight()
light.direction.setValue(coin.SbVec3f(-0.5, 0.3, -0.8))
light.intensity.setValue(0.8)
root.insertChild(light, 0)

cam.nearDistance.setValue(1.0)
cam.farDistance.setValue(300.0)

# Camera angles chosen to show all bracket features:
# Front-right: shows base plate, wall front, holes, slot
# Rear-left: shows rib, fillet, wall back
VIEWS = [("bracket_front.png", coin.SbVec3f(100, -60, 50)), ("bracket_rear.png", coin.SbVec3f(-80, 80, 60))]

for filename, cam_pos in VIEWS:
    cam.position.setValue(cam_pos)
    cam.pointAt(coin.SbVec3f(40, 25, 20))  # Center of bracket geometry
    pump(2)
    view.fitAll()
    pump(1)

    output_path = os.path.join(outdir, filename)  # noqa: PTH118 — FreeCAD API expects str
    view.saveImage(output_path, 800, 600, "Current")
    print(f"Rendered: {output_path}")

os._exit(0)  # Skip Qt cleanup to avoid potential segfault under xvfb
