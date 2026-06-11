"""Verify an FCStd file loads cleanly in a fresh FreeCAD instance.

Opens the file, recomputes, and checks for errors. Exits 0 on success,
1 on load errors. Writes a JSON report to OUTDIR/load_report.json.

Usage:
  QT_QPA_PLATFORM=offscreen INPUT=/path/to/model.FCStd OUTDIR=/tmp/out freecadcmd verify_fcstd_load.py
"""

import json
import os
import sys
from pathlib import Path

import FreeCAD

input_path = os.environ["INPUT"]
outdir = Path(os.environ.get("OUTDIR", "."))


def log(msg):
    print(msg, file=sys.stderr, flush=True)


doc = FreeCAD.openDocument(input_path)
log(f"Opened {input_path}: {len(doc.Objects)} objects")

# Recompute and collect any errors
doc.recompute()
errors = []
for obj in doc.Objects:
    if obj.isValid():
        continue
    status = getattr(obj, "StatusMessage", "unknown error")
    errors.append({"name": obj.Name, "type": obj.TypeId, "status": status})
    log(f"  ERROR: {obj.Name} ({obj.TypeId}): {status}")

# Check the final body shape if present
bodies = [o for o in doc.Objects if o.TypeId == "PartDesign::Body"]
for body in bodies:
    if body.Shape.isValid():
        log(f"  Body {body.Name}: valid, volume={body.Shape.Volume:.1f} mm3")
    else:
        errors.append({"name": body.Name, "type": body.TypeId, "status": "invalid shape"})
        log(f"  ERROR: Body {body.Name} has invalid shape")

report = {"input": input_path, "object_count": len(doc.Objects), "errors": errors, "ok": len(errors) == 0}
report_path = outdir / "load_report.json"
report_path.write_text(json.dumps(report, indent=2))
log(f"Report: {report_path} (ok={report['ok']})")

FreeCAD.closeDocument(doc.Name)
sys.exit(0 if report["ok"] else 1)
