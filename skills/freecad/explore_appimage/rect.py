"""
Minimal FreeCAD script: create a 100x50 rectangle and export to DXF.

Designed to run headlessly (no TechDraw, no GUI). Tested with:
  freecadcmd rect.py

Output written to OUTDIR env var (default: /tmp).
"""

import importlib
import importlib.util
import os
import sys
from pathlib import Path

import FreeCAD as App
import importDXF


def _fix_freecad_stub_modules() -> None:
    """Reload FreeCAD C extensions that are shadowed by Bazel-generated stubs.

    On RBE, Bazel auto-generates __init__.py stub files for directories in the
    runfiles tree. This causes Python to load the stubs (e.g. usr/Mod/Part/)
    instead of the C extensions (usr/lib/Part.so), leaving C-only APIs like
    Part.makePolygon and Import.writeDXFObject unavailable.

    Fix: for each *.so in usr/lib/ that has a corresponding sys.modules entry
    loaded from a /Mod/ stub path, remove the stub and reload the C extension.
    """
    prefix = Path(os.environ.get("PYTHONHOME", "")) / "lib"
    for so_path in sorted(prefix.glob("*.so")):
        name = so_path.stem
        mod = sys.modules.get(name)
        if mod is None:
            continue
        mod_file = getattr(mod, "__file__", "") or ""
        if "/Mod/" not in mod_file:
            continue
        sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location(name, str(so_path))
        new_mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = new_mod
        spec.loader.exec_module(new_mod)


_fix_freecad_stub_modules()

# These must come after _fix_freecad_stub_modules(); use importlib to avoid E402.
Part = importlib.import_module("Part")

outdir = Path(os.environ.get("OUTDIR", "/tmp"))
outdir.mkdir(parents=True, exist_ok=True)

doc = App.newDocument("RectTest")

# A closed rectangular wire: 100 x 50
pts = [
    App.Vector(0, 0, 0),
    App.Vector(100, 0, 0),
    App.Vector(100, 50, 0),
    App.Vector(0, 50, 0),
    App.Vector(0, 0, 0),  # close
]
wire = Part.makePolygon(pts)
face = Part.Face(wire)

feat = doc.addObject("Part::Feature", "Rect")
feat.Shape = face
doc.recompute()

out_dxf = str(outdir / "rect.dxf")
importDXF.export([feat], out_dxf)
print(f"Wrote {out_dxf} ({Path(out_dxf).stat().st_size} bytes)", file=sys.stderr)

out_fcstd = str(outdir / "rect.FCStd")
doc.saveAs(out_fcstd)
print(f"Wrote {out_fcstd} ({Path(out_fcstd).stat().st_size} bytes)", file=sys.stderr)

os._exit(0)
