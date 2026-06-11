"""
Render a Part Design body with each face colored uniquely for visual debugging.

Assigns each face of the Body's Tip shape a distinct color via per-face
DiffuseColor, then renders from the standard isometric angle. The output PNG
shows which Face index corresponds to which geometric surface.

This helps identify face indices for sketch attachment (AttachmentSupport)
and for verifying face-finding predicates in build scripts.

Usage:
  INPUT=/work/model.FCStd OUTDIR=/output \
    DISPLAY=:99 freecad render_debug_faces.py
"""

import os

import FreeCAD
import FreeCADGui

from skills.freecad.examples.freecad_helpers import init_gui, log, pump, run_gui_script

input_path = os.environ.get("INPUT", "bearing_block.FCStd")
outdir = os.environ.get("OUTDIR", ".")

qapp = init_gui()


def _generate_colors(n):
    """Generate n visually distinct colors using golden-ratio hue spacing."""
    colors = []
    for i in range(n):
        # Golden ratio conjugate for even hue distribution
        hue = (i * 0.618033988749895) % 1.0
        # Convert HSV to RGB (S=0.7, V=0.9 for visible pastel-ish colors)
        s, v = 0.7, 0.9
        h_i = int(hue * 6)
        f = hue * 6 - h_i
        p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
        rgb_map = {0: (v, t, p), 1: (q, v, p), 2: (p, v, t), 3: (p, q, v), 4: (t, p, v), 5: (v, p, q)}
        colors.append(rgb_map[h_i % 6])
    return colors


def _main() -> None:
    from pivy import coin  # noqa: PLC0415 — must import after GUI binary starts

    doc = FreeCAD.openDocument(input_path)
    FreeCAD.setActiveDocument(doc.Name)
    FreeCADGui.ActiveDocument = FreeCADGui.getDocument(doc.Name)
    pump(qapp, 2)

    # Find the shape to colorize — prefer PartDesign::Body Tip, fall back to Part::Feature
    target = None
    for obj in doc.Objects:
        if obj.TypeId == "PartDesign::Body" and hasattr(obj, "Tip") and obj.Tip:
            target = obj
            break
    if target is None:
        for obj in doc.Objects:
            if obj.TypeId == "Part::Feature":
                target = obj
                break
    assert target, "No PartDesign::Body or Part::Feature found in document"

    # Hide non-3D objects (TechDraw, Sketcher, Spreadsheet) to avoid viewport issues
    # when loading FCStd files that contain TechDraw views.
    _3d_types = {"Part::Feature", "PartDesign::Body"}
    for obj in doc.Objects:
        vo = getattr(obj, "ViewObject", None)
        if vo is None or not hasattr(vo, "Visibility"):
            continue
        if obj.TypeId in _3d_types:
            vo.Visibility = True
        else:
            vo.Visibility = False

    # For PartDesign::Body, the Tip must be visible and configured for shaded rendering.
    # The Body delegates rendering to its Tip — setting DisplayMode on the Body has no effect.
    if target.TypeId == "PartDesign::Body":
        tip = target.Tip
        tip_vo = tip.ViewObject
        tip_vo.Visibility = True
        tip_vo.DisplayMode = "Shaded"
        tip_vo.Lighting = "Two side"
        tip_vo.Transparency = 0
        shape = tip.Shape
    else:
        vo = target.ViewObject
        vo.Visibility = True
        vo.DisplayMode = "Shaded"
        vo.Lighting = "Two side"
        shape = target.Shape

    n_faces = len(shape.Faces)
    log(f"Shape has {n_faces} faces")

    # Create a temporary Part::Feature to hold the colored shape.
    # DiffuseColor on PartDesign feature ViewObjects is unreliable because the Body
    # overrides child view properties. A standalone Part::Feature with the final
    # shape accepts per-face DiffuseColor reliably (confirmed in FreeCAD's own
    # ColorPerFaceTest in src/Mod/Part/Gui/Tests/TestPartGui.py).
    color_feat = doc.addObject("Part::Feature", "DebugColoredShape")
    color_feat.Shape = shape
    doc.recompute()  # must recompute before setting view properties
    if target.TypeId == "PartDesign::Body":
        target.ViewObject.Visibility = False
    color_feat.ViewObject.Visibility = True
    color_feat.ViewObject.DisplayMode = "Shaded"
    color_feat.ViewObject.Lighting = "Two side"

    colors = _generate_colors(n_faces)
    # DiffuseColor format: list of (R, G, B, A) tuples, floats 0.0-1.0.
    # Three gotchas discovered by reading FreeCAD source (ViewProviderPartExtPyImp.cpp,
    # Base/Color.cpp, and ColorPerFaceTest in src/Mod/Part/Gui/Tests/):
    #
    # 1. Alpha 1.0 = opaque, 0.0 = transparent (OPPOSITE of CSS/WebGL convention).
    #    Internally: transparency = 1.0 - alpha. Using alpha=0.0 renders invisible.
    #
    # 2. List length MUST exactly equal len(Shape.Faces). If it doesn't match, the
    #    Coin3D PER_PART binding silently falls through and the shape keeps its
    #    previous single color — no error, no effect.
    #
    # 3. Call ViewObject.update() after setting DiffuseColor to force a redraw.
    assert len(colors) == n_faces, f"Color count {len(colors)} != face count {n_faces}"
    diffuse = [(r, g, b, 1.0) for r, g, b in colors]
    color_feat.ViewObject.DiffuseColor = diffuse
    color_feat.ViewObject.update()

    pump(qapp, 2)

    # Log face info for cross-referencing with the rendered image
    for i, f in enumerate(shape.Faces, 1):
        r, g, b = colors[i - 1]
        com = f.CenterOfMass
        surface_type = type(f.Surface).__name__
        log(
            f"  Face{i}: {surface_type} area={f.Area:.1f} "
            f"center=({com.x:.1f},{com.y:.1f},{com.z:.1f}) "
            f"color=({r:.2f},{g:.2f},{b:.2f})"
        )

    # Set up viewport
    view = FreeCADGui.ActiveDocument.ActiveView

    param = FreeCAD.ParamGet("User parameter:BaseApp/Preferences/View")
    param.SetBool("Gradient", False)
    param.SetUnsigned("BackgroundColor", 0xFFFFFFFF)

    cam = view.getCameraNode()
    if cam.getTypeId().getName() == "SoOrthographicCamera":
        FreeCADGui.runCommand("Std_PerspectiveCamera", 0)
        pump(qapp, 1)
        cam = view.getCameraNode()

    cam.position.setValue(coin.SbVec3f(60, -40, 45))
    cam.pointAt(coin.SbVec3f(0, 0, 0))
    cam.nearDistance.setValue(1.0)
    cam.farDistance.setValue(500.0)

    root = view.getSceneGraph()
    light = coin.SoDirectionalLight()
    light.direction.setValue(coin.SbVec3f(-0.5, 0.3, -0.8))
    light.intensity.setValue(0.8)
    root.insertChild(light, 0)

    pump(qapp, 2)
    view.fitAll()
    pump(qapp, 1)

    output_path = os.path.join(outdir, "debug_faces.png")  # noqa: PTH118
    view.saveImage(output_path, 800, 600, "Current")
    log(f"Saved: {output_path}")


run_gui_script(qapp, _main)
