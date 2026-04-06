"""
Render a FreeCAD FCStd file to PNG from multiple camera angles.

Produces one PNG per camera angle. Default angles show front-right and
back-left isometric views. Custom angles can be provided via ANGLES env var
as JSON: [{"name": "front", "pos": [60, -40, 45]}, ...].

Runs inside freecadcmd under xvfb (needs Qt/OpenGL for 3D viewport rendering).
Reads INPUT env var for the FCStd path and OUTDIR for output directory.

Usage:
  INPUT=/work/model.FCStd OUTDIR=/output \
    xvfb-run -a -s "-screen 0 1024x768x24" freecadcmd render_multi_angle.py
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import FreeCAD as App
import FreeCADGui as Gui
from freecad_helpers import init_gui, log, pump

Gui.showMainWindow()
qapp = init_gui()

from pivy import coin  # noqa: E402 — must import after Gui.showMainWindow()

_DEFAULT_ANGLES = [{"name": "front_right", "pos": [60, -40, 45]}, {"name": "back_left", "pos": [-60, 40, 45]}]


input_path = os.environ.get("INPUT", "bearing_block.FCStd")
outdir = os.environ.get("OUTDIR", ".")
angles = json.loads(os.environ.get("ANGLES", "null")) or _DEFAULT_ANGLES

# === Load document ===
doc = App.openDocument(input_path)
App.setActiveDocument(doc.Name)
Gui.ActiveDocument = Gui.getDocument(doc.Name)

pump(qapp, 2)

# Configure view properties for shaded rendering.
# GOTCHA for PartDesign::Body: the Body delegates rendering to its Tip
# feature. Setting DisplayMode on the Body itself does nothing — you must
# configure the Tip's ViewObject. The Body just needs Visibility=True.
#
# Hide non-3D objects (TechDraw pages, Sketcher sketches, Spreadsheets)
# to avoid "failed to create projection CS" warnings during recompute.
_3D_TYPES = {"Part::Feature", "PartDesign::Body"}
_SHADE_TYPES = {"Part::Feature"}

for obj in doc.Objects:
    vo = getattr(obj, "ViewObject", None)
    if vo is None or not hasattr(vo, "Visibility"):
        continue
    if obj.TypeId in _3D_TYPES:
        vo.Visibility = True
        if obj.TypeId in _SHADE_TYPES:
            vo.DisplayMode = "Shaded"
            vo.ShapeColor = (0.40, 0.55, 0.70)
            vo.Lighting = "One side"
            vo.Transparency = 0
    else:
        vo.Visibility = False

# For PartDesign bodies, configure the Tip feature for shaded rendering
for obj in doc.Objects:
    if obj.TypeId == "PartDesign::Body" and hasattr(obj, "Tip") and obj.Tip:
        tip_vo = obj.Tip.ViewObject
        tip_vo.Visibility = True
        tip_vo.DisplayMode = "Shaded"
        tip_vo.ShapeColor = (0.40, 0.55, 0.70)
        tip_vo.Lighting = "One side"
        tip_vo.Transparency = 0

pump(qapp, 2)

view = Gui.ActiveDocument.ActiveView

# Set white background
param = App.ParamGet("User parameter:BaseApp/Preferences/View")
param.SetBool("Gradient", False)
param.SetUnsigned("BackgroundColor", 0xFFFFFFFF)

# Use perspective projection
cam = view.getCameraNode()
if cam.getTypeId().getName() == "SoOrthographicCamera":
    Gui.runCommand("Std_PerspectiveCamera", 0)
    pump(qapp, 1)
    cam = view.getCameraNode()

# Add directional light for face contrast
root = view.getSceneGraph()
light = coin.SoDirectionalLight()
light.direction.setValue(coin.SbVec3f(-0.5, 0.3, -0.8))
light.intensity.setValue(0.8)
root.insertChild(light, 0)

pump(qapp, 1)

stem = Path(input_path).stem

for angle in angles:
    name = angle["name"]
    pos = angle["pos"]

    cam.position.setValue(coin.SbVec3f(*pos))
    cam.pointAt(coin.SbVec3f(0, 0, 0))
    cam.nearDistance.setValue(1.0)
    cam.farDistance.setValue(500.0)

    pump(qapp, 1)
    view.fitAll()
    pump(qapp, 1)

    output_path = os.path.join(outdir, f"{stem}_{name}.png")  # noqa: PTH118
    view.saveImage(output_path, 800, 600, "Current")
    log(f"Rendered: {output_path}")

os._exit(0)
