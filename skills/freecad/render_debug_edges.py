"""
Render TechDraw visible edges with unique colors for visual debugging.

Loads an FCStd containing TechDraw views, draws each visible edge with a
distinct color, and labels it with its edge index. Produces one PNG per view.

This helps identify which Edge index corresponds to which geometric feature
when writing edge-matching predicates for entity-referenced dimensions.

Usage:
  INPUT=/work/model.FCStd OUTDIR=/output \
    xvfb-run -a -s "-screen 0 1024x768x24" freecadcmd render_debug_edges.py
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

try:
    from PySide6 import QtCore, QtGui
except ImportError:
    from PySide2 import QtCore, QtGui

input_path = os.environ.get("INPUT", "bearing_block.FCStd")
outdir = os.environ.get("OUTDIR", ".")
view_filter = json.loads(os.environ.get("VIEWS", "null"))


# Use osifont bundled with FreeCAD's TechDraw module. This font ships with every
# FreeCAD installation (it's the default TechDraw dimension font), so it's a safe
# default that doesn't depend on system font availability. Using it ensures debug
# renders look identical across machines and Docker workers.
_OSIFONT_PATH = os.path.join(  # noqa: PTH118
    App.getResourceDir(), "Mod", "TechDraw", "Resources", "fonts", "osifont-lgpl3fe.ttf"
)
_FONT_DB = QtGui.QFontDatabase
if os.path.exists(_OSIFONT_PATH):  # noqa: PTH110 — FreeCAD env
    _FONT_DB.addApplicationFont(_OSIFONT_PATH)
    _FONT_FAMILY = "osifont"
    log(f"Using osifont from {_OSIFONT_PATH}")
else:
    _FONT_FAMILY = "Sans"
    log(f"WARNING: osifont not found at {_OSIFONT_PATH}, falling back to Sans")


def _edge_dx(e):
    if len(e.Vertexes) >= 2:
        return abs(e.Vertexes[1].Point.x - e.Vertexes[0].Point.x)
    return 0


def _edge_dy(e):
    if len(e.Vertexes) >= 2:
        return abs(e.Vertexes[1].Point.y - e.Vertexes[0].Point.y)
    return 0


def _make_transform(edges, width, height, margin):
    """Build a model→pixel coordinate transform from the edge bounding box."""
    all_x, all_y = [], []
    for e in edges:
        for v in e.Vertexes:
            all_x.append(v.Point.x)
            all_y.append(v.Point.y)
    min_x, max_x = min(all_x), max(all_x)
    min_y = min(all_y)
    span_x = max_x - min_x or 1
    span_y = max(all_y) - min_y or 1
    scale = min((width - 2 * margin) / span_x, (height - 2 * margin) / span_y)

    def to_px(x, y, _min_x=min_x, _min_y=min_y, _scale=scale, _margin=margin, _height=height):
        px = _margin + (x - _min_x) * _scale
        py = _height - _margin - (y - _min_y) * _scale
        return QtCore.QPointF(px, py)

    return to_px


# 20 distinguishable colors
_COLORS = [
    (255, 0, 0),
    (0, 0, 255),
    (0, 180, 0),
    (255, 165, 0),
    (128, 0, 128),
    (0, 200, 200),
    (255, 20, 147),
    (139, 69, 19),
    (0, 128, 0),
    (255, 215, 0),
    (70, 130, 180),
    (220, 20, 60),
    (0, 100, 0),
    (255, 140, 0),
    (75, 0, 130),
    (0, 0, 128),
    (199, 21, 133),
    (34, 139, 34),
    (255, 99, 71),
    (0, 139, 139),
]

doc = App.openDocument(input_path)
App.setActiveDocument(doc.Name)
doc.recompute()
pump(qapp, 5)

views = [obj for obj in doc.Objects if obj.TypeId == "TechDraw::DrawViewPart"]
if view_filter:
    views = [v for v in views if v.Name in view_filter]

for view in views:
    edges = view.getVisibleEdges()
    if not edges:
        log(f"{view.Name}: 0 visible edges, skipping")
        continue

    log(f"{view.Name}: {len(edges)} visible edges")

    width, height = 1800, 1350
    margin = 80
    img = QtGui.QImage(width, height, QtGui.QImage.Format.Format_RGB32)
    img.fill(QtGui.QColor(255, 255, 255))

    painter = QtGui.QPainter(img)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

    to_px = _make_transform(edges, width, height, margin)

    for i, e in enumerate(edges):
        color = _COLORS[i % len(_COLORS)]
        pen = QtGui.QPen(QtGui.QColor(*color), 2)
        painter.setPen(pen)

        verts = e.Vertexes
        if len(verts) >= 2:
            if hasattr(e, "discretize"):
                try:
                    points = e.discretize(20)
                    for j in range(len(points) - 1):
                        painter.drawLine(to_px(points[j].x, points[j].y), to_px(points[j + 1].x, points[j + 1].y))
                except Exception as exc:
                    log(f"{view.Name}: discretize failed on edge E{i}, falling back to straight line: {exc}")
                    painter.drawLine(
                        to_px(verts[0].Point.x, verts[0].Point.y), to_px(verts[1].Point.x, verts[1].Point.y)
                    )
            else:
                painter.drawLine(to_px(verts[0].Point.x, verts[0].Point.y), to_px(verts[1].Point.x, verts[1].Point.y))

        # Label at midpoint
        if len(verts) >= 2:
            mid_x = (verts[0].Point.x + verts[1].Point.x) / 2
            mid_y = (verts[0].Point.y + verts[1].Point.y) / 2
        elif len(verts) == 1:
            mid_x, mid_y = verts[0].Point.x, verts[0].Point.y
        else:
            continue

        curve_type = type(e.Curve).__name__
        dx, dy = _edge_dx(e), _edge_dy(e)
        label = f"E{i}"
        detail = f"{curve_type} dx={dx:.0f} dy={dy:.0f}"
        if hasattr(e.Curve, "Radius"):
            detail += f" R={e.Curve.Radius:.1f}"

        pos = to_px(mid_x, mid_y)
        pos = QtCore.QPointF(pos.x() + 4, pos.y() - 4)

        painter.setFont(QtGui.QFont(_FONT_FAMILY, 16, QtGui.QFont.Weight.Bold))
        painter.drawText(pos, label)
        detail_pos = QtCore.QPointF(pos.x(), pos.y() + 20)
        painter.setFont(QtGui.QFont(_FONT_FAMILY, 12))
        painter.drawText(detail_pos, detail)

    painter.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0), 1))
    painter.setFont(QtGui.QFont(_FONT_FAMILY, 22, QtGui.QFont.Weight.Bold))
    painter.drawText(QtCore.QPointF(10, 25), f"{view.Name} — {len(edges)} edges")

    painter.end()

    output_path = os.path.join(outdir, f"{view.Name}_debug_edges.png")  # noqa: PTH118
    img.save(output_path)
    log(f"Saved: {output_path}")

os._exit(0)
