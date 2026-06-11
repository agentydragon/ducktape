# FreeCAD Part Design Reference

The Part Design workbench builds solid models as a feature tree inside a `PartDesign::Body`
where each operation (Pad, Pocket, Fillet, Chamfer) builds on the previous one.
See <build_bearing_block.py> for a full example, and <build_bearing_block_techdraw.py> for
a multi-view TechDraw drawing.

## Body and feature tree

```python
body = doc.addObject("PartDesign::Body", "Body")
```

All Part Design features are added via `body.newObject()`. The Body maintains a linear feature
tree — each feature modifies the shape produced by the previous one. The final shape is
`body.Shape` (equivalent to `body.Tip.Shape`).

## Sketch attachment

Sketches must be attached to a plane or face via `AttachmentSupport` + `MapMode`.

**Attaching to origin planes.** The Body's Origin provides standard planes:

```python
# OriginFeatures indices:
#   [0]=X_Axis, [1]=Y_Axis, [2]=Z_Axis,
#   [3]=XY_Plane, [4]=XZ_Plane, [5]=YZ_Plane
sk = body.newObject("Sketcher::SketchObject", "BaseSketch")
sk.AttachmentSupport = [(body.Origin.OriginFeatures[3], "")]  # XY_Plane
sk.MapMode = "FlatFace"
```

**Attaching to a feature face.** Face indices like `"Face6"` are **topology-dependent** —
they shift when the feature tree changes. Use geometric properties to find the right face:

```python
# Find the face with highest Z center-of-mass = top face
shape = pad.Shape
top_face_idx = max(
    range(1, len(shape.Faces) + 1),
    key=lambda i: shape.Faces[i - 1].CenterOfMass.z,
)
sk = body.newObject("Sketcher::SketchObject", "BossSketch")
sk.AttachmentSupport = [(pad, f"Face{top_face_idx}")]
sk.MapMode = "FlatFace"
```

When features have pockets/bores, filter by both position AND area — the largest planar face
at the target Z is the base face with holes cut in it:

```python
candidate_faces = []
for i, f in enumerate(shape.Faces, 1):
    if abs(f.CenterOfMass.z - target_z) < 0.1 and f.Surface.isPlanar():
        candidate_faces.append((i, f))
face_idx = max(candidate_faces, key=lambda x: x[1].Area)[0]
```

## Pad (extrude)

```python
pad = body.newObject("PartDesign::Pad", "BasePad")
pad.Profile = sketch
pad.setExpression("Length", "Params.BaseHeight")
doc.recompute()
```

`setExpression` works without setting `.Length` first.

## Pocket (cut)

```python
pocket = body.newObject("PartDesign::Pocket", "CentralBore")
pocket.Profile = bore_sketch
pocket.Type = 1  # 0=Dimension, 1=ThroughAll, 2=ToFirst, 3=ToFace, 4=TwoDimensions
doc.recompute()
```

## Fillet and Chamfer

Select edges by geometric properties (edge indices are topology-dependent):

```python
# Find circular edge where boss cylinder meets the base
shape = previous_feature.Shape
boss_r = BOSS_D / 2
fillet_edges = []
for i, e in enumerate(shape.Edges, 1):
    if not isinstance(e.Curve, Part.Circle):
        continue
    if abs(e.Curve.Radius - boss_r) < 0.5 and abs(e.Curve.Center.z - BASE_H) < 0.5:
        fillet_edges.append(f"Edge{i}")

fillet = body.newObject("PartDesign::Fillet", "BossFillet")
fillet.Base = (previous_feature, fillet_edges)
fillet.setExpression("Radius", "Params.BossFilletRadius")
```

The `Base` property takes `(feature_object, ["Edge1", "Edge2", ...])`. For straight edges,
filter by `Part.Line` type and vertex positions:

```python
chamfer_edges = []
for i, e in enumerate(shape.Edges, 1):
    if not isinstance(e.Curve, Part.Line):
        continue
    v0, v1 = e.Vertexes[0].Point, e.Vertexes[1].Point
    if abs(v0.z - BASE_H) < 0.1 and abs(v1.z - BASE_H) < 0.1:
        on_perimeter = abs(abs(v0.x) - BASE_L / 2) < 0.1 or abs(abs(v0.y) - BASE_W / 2) < 0.1
        if on_perimeter:
            chamfer_edges.append(f"Edge{i}")

chamfer = body.newObject("PartDesign::Chamfer", "BaseChamfer")
chamfer.Base = (previous_feature, chamfer_edges)
chamfer.setExpression("Size", "Params.BaseChamfer")
```

## Parametric principle: expressions, not Python values

The Python script runs **once** to generate an FCStd file. The FCStd must encode all
dimensional relationships internally so they survive editing in the FreeCAD GUI.

```python
# WRONG — FCStd stores 5.0, not a reference to the spreadsheet.
fillet.Radius = float(sheet.get("B10"))

# RIGHT — FCStd stores "Params.BossFilletRadius" as an expression.
fillet.setExpression("Radius", "Params.BossFilletRadius")
```

Python-computed values are acceptable only for initial sketch geometry placement (approximate
starting points that get overridden by constraints). For all feature properties (`Length`,
`Radius`, `Size`, etc.), use `setExpression` exclusively.

## Body visibility for rendering

A `PartDesign::Body` delegates rendering to its Tip feature — setting `DisplayMode = "Shaded"`
on the Body itself has no effect. Configure the Tip's ViewObject:

```python
for obj in doc.Objects:
    if obj.TypeId == "PartDesign::Body" and hasattr(obj, "Tip") and obj.Tip:
        obj.ViewObject.Visibility = True
        tip_vo = obj.Tip.ViewObject
        tip_vo.Visibility = True
        tip_vo.DisplayMode = "Shaded"
        tip_vo.ShapeColor = (0.75, 0.75, 0.80)
        tip_vo.Lighting = "One side"
```

**Documents with TechDraw views:** Loading an FCStd with `DrawViewPart` objects triggers
view recomputation. If any view has a broken projection CS, FreeCAD can crash the 3D
viewport. Before accessing the 3D viewport for rendering, hide all non-3D objects:

```python
_3D_TYPES = {"Part::Feature", "PartDesign::Body"}
for obj in doc.Objects:
    vo = getattr(obj, "ViewObject", None)
    if vo and hasattr(vo, "Visibility"):
        vo.Visibility = obj.TypeId in _3D_TYPES
```

See <render_multi_angle.py> for rendering Part Design models from multiple camera angles.
