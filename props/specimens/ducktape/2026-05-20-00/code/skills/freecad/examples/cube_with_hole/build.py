"""
Build a cube with a cylindrical hole drilled through the center, save as FCStd.

Runs inside freecadcmd (no GUI needed for modeling).
Output directory is read from OUTDIR env var (default: current directory).

Usage:
  OUTDIR=/tmp/out freecadcmd build_cube_with_hole.py
"""

import os
from pathlib import Path

import FreeCAD
import Part

from skills.freecad.examples.freecad_helpers import log

outdir = os.environ.get("OUTDIR", ".")


log("starting build")

# === Parameters ===
CUBE_SIZE = 20.0  # mm
HOLE_RADIUS = 5.0  # mm

# === Geometry ===
doc = FreeCAD.newDocument("CubeWithHole")

cube = Part.makeBox(CUBE_SIZE, CUBE_SIZE, CUBE_SIZE, FreeCAD.Vector(-CUBE_SIZE / 2, -CUBE_SIZE / 2, -CUBE_SIZE / 2))

# Cylinder through the full cube height along Z axis, centered on X/Y
cylinder = Part.makeCylinder(
    HOLE_RADIUS,
    CUBE_SIZE + 2,  # slightly longer than cube to ensure clean cut
    FreeCAD.Vector(0, 0, -CUBE_SIZE / 2 - 1),
    FreeCAD.Vector(0, 0, 1),
)

result = cube.cut(cylinder)

feat = doc.addObject("Part::Feature", "CubeWithHole")
feat.Shape = result
doc.recompute()

# === Export ===
log("saving FCStd")
fcstd_path = os.path.join(outdir, "cube_with_hole.FCStd")  # noqa: PTH118 — FreeCAD API expects str
doc.saveAs(fcstd_path)
log(f"FCStd: {Path(fcstd_path).stat().st_size} bytes — done")
