"""
Render a FreeCAD FCStd file to PNG from multiple camera angles.

Produces one PNG per camera angle. Default angles show front-right and
back-left isometric views. Custom angles can be provided via ANGLES env var
as JSON: [{"name": "front", "pos": [60, -40, 45]}, ...].

Runs via the freecad GUI binary with Xvfb (needs Qt/OpenGL for 3D viewport
rendering). Reads INPUT env var for the FCStd path and OUTDIR for output
directory.

Usage:
  INPUT=/work/model.FCStd OUTDIR=/output \
    DISPLAY=:99 freecad render_multi_angle.py
"""

import json
import os
from pathlib import Path

import FreeCAD
import FreeCADGui

from skills.freecad.examples.freecad_helpers import init_gui, log, pump, run_gui_script

_DEFAULT_ANGLES = [{"name": "front_right", "pos": [60, -40, 45]}, {"name": "back_left", "pos": [-60, 40, 45]}]

input_path = os.environ.get("INPUT", "bearing_block.FCStd")
outdir = os.environ.get("OUTDIR", ".")
angles = json.loads(os.environ.get("ANGLES", "null")) or _DEFAULT_ANGLES

qapp = init_gui()


def _main() -> None:
    from pivy import coin  # noqa: PLC0415 — must import after GUI binary starts

    # === Load document ===
    doc = FreeCAD.openDocument(input_path)
    FreeCAD.setActiveDocument(doc.Name)
    FreeCADGui.ActiveDocument = FreeCADGui.getDocument(doc.Name)

    pump(qapp, 2)

    # Configure view properties for shaded rendering.
    # GOTCHA for PartDesign::Body: the Body delegates rendering to its Tip
    # feature. Setting DisplayMode on the Body itself does nothing — you must
    # configure the Tip's ViewObject. The Body just needs Visibility=True.
    #
    # Hide non-3D objects (TechDraw pages, Sketcher sketches, Spreadsheets)
    # to avoid "failed to create projection CS" warnings during recompute.
    _3d_types = {"Part::Feature", "PartDesign::Body"}
    _shade_types = {"Part::Feature"}

    for obj in doc.Objects:
        vo = getattr(obj, "ViewObject", None)
        if vo is None or not hasattr(vo, "Visibility"):
            continue
        if obj.TypeId in _3d_types:
            vo.Visibility = True
            if obj.TypeId in _shade_types:
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

    view = FreeCADGui.ActiveDocument.ActiveView

    # Set white background
    param = FreeCAD.ParamGet("User parameter:BaseApp/Preferences/View")
    param.SetBool("Gradient", False)
    param.SetUnsigned("BackgroundColor", 0xFFFFFFFF)

    # Use perspective projection
    cam = view.getCameraNode()
    if cam.getTypeId().getName() == "SoOrthographicCamera":
        FreeCADGui.runCommand("Std_PerspectiveCamera", 0)
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


run_gui_script(qapp, _main)
