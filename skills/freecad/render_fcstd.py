"""
Render a FreeCAD FCStd file to PNG with 3D perspective and lighting.

Runs inside freecadcmd under xvfb (needs Qt/OpenGL for 3D viewport rendering).
Reads INPUT env var for the FCStd path and OUTDIR for output directory.

Usage:
  INPUT=/work/model.FCStd OUTDIR=/output xvfb-run -a -s "-screen 0 1024x768x24" freecadcmd render_fcstd.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, "/work")  # freecad_helpers.py is mounted alongside this script

import FreeCAD as App
import FreeCADGui as Gui
from freecad_helpers import init_gui, log, pump

Gui.showMainWindow()
qapp = init_gui()

from pivy import coin  # noqa: E402, I001 — must import after Gui.showMainWindow()


input_path = os.environ.get("INPUT", "cube_with_hole.FCStd")
outdir = os.environ.get("OUTDIR", ".")

log("loading document")
doc = App.openDocument(input_path)
App.setActiveDocument(doc.Name)
Gui.ActiveDocument = Gui.getDocument(doc.Name)

log("pump for document load")
pump(qapp, 2)

log("configuring view properties")
# Configure Part::Feature view properties for shaded rendering.
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

# === Configure view ===
# Set white background
param = App.ParamGet("User parameter:BaseApp/Preferences/View")
param.SetBool("Gradient", False)
param.SetUnsigned("BackgroundColor", 0xFFFFFFFF)

# Use perspective projection for depth
cam = view.getCameraNode()
cam_type = cam.getTypeId().getName()
if cam_type == "SoOrthographicCamera":
    Gui.runCommand("Std_PerspectiveCamera", 0)
    pump(qapp, 1)
    cam = view.getCameraNode()

# Set camera to an angle where faces are visibly different
# (elevated azimuth so top, front-right, and right faces get different lighting)
# Position camera at an angle that shows three distinct faces
# The key is that the camera angle differs from the light direction
cam.position.setValue(coin.SbVec3f(40, -30, 35))
cam.pointAt(coin.SbVec3f(0, 0, 0))
cam.nearDistance.setValue(1.0)
cam.farDistance.setValue(200.0)

# Set up a directional light from upper-left to create face contrast
# FreeCAD's default headlight follows the camera; add a separate light
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

log("saving image")
# === Save image ===
output_name = Path(input_path).stem + ".png"
output_path = os.path.join(outdir, output_name)  # noqa: PTH118 — FreeCAD API expects str
view.saveImage(output_path, 800, 600, "Current")
log(f"rendered: {output_path} — done")

os._exit(0)  # Qt6 TLS crash during shutdown — see debug/qt_shutdown_segfault.md
