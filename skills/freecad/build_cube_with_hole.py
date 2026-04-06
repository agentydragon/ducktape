"""
Build a cube with a cylindrical hole drilled through the center, save as FCStd.

Runs inside freecadcmd (no GUI needed for modeling).
Output directory is read from OUTDIR env var (default: current directory).

Usage:
  OUTDIR=/tmp/out freecadcmd build_cube_with_hole.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import FreeCAD as App
import Part
from freecad_helpers import log

outdir = os.environ.get("OUTDIR", ".")


log("starting build")

# === Parameters ===
CUBE_SIZE = 20.0  # mm
HOLE_RADIUS = 5.0  # mm

# === Geometry ===
doc = App.newDocument("CubeWithHole")

cube = Part.makeBox(CUBE_SIZE, CUBE_SIZE, CUBE_SIZE, App.Vector(-CUBE_SIZE / 2, -CUBE_SIZE / 2, -CUBE_SIZE / 2))

# Cylinder through the full cube height along Z axis, centered on X/Y
cylinder = Part.makeCylinder(
    HOLE_RADIUS,
    CUBE_SIZE + 2,  # slightly longer than cube to ensure clean cut
    App.Vector(0, 0, -CUBE_SIZE / 2 - 1),
    App.Vector(0, 0, 1),
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
