"""Low-level LightBurn .lbrn2 XML writer.

Each LightBurn entity is modelled as a typed Python dataclass with a
to_element() method returning an xml.etree.ElementTree.Element.

See FORMAT_RESEARCH.md for format documentation and sources.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from xml.etree import ElementTree as ET

LBRN2_APP_VERSION = "1.6.00"
LBRN2_FORMAT_VERSION = "1"


# ── Enums ──────────────────────────────────────────────────────────────────────


class CutMode(StrEnum):
    """LightBurn layer mode."""

    CUT = "Cut"  # Line/cut mode: follows vector outlines
    SCAN = "Scan"  # Fill/engrave mode: raster scan
    SCAN_AND_CUT = "Scan+Cut"  # Both fill and outline


class HAlign(StrEnum):
    """Horizontal text anchor alignment."""

    LEFT = "0"
    CENTER = "1"
    RIGHT = "2"


class VAlign(StrEnum):
    """Vertical text anchor alignment."""

    TOP = "0"
    CENTER = "1"
    BOTTOM = "2"


# ── XForm ─────────────────────────────────────────────────────────────────────


@dataclass
class XForm:
    """2D affine transformation matrix (LightBurn's 6-value XForm format).

    Represents the transform:
        x' = a*x + c*y + tx
        y' = b*x + d*y + ty

    As a matrix:
        | a  c  tx |
        | b  d  ty |
        | 0  0   1 |

    Coordinate system: X right, Y down (screen convention).
    """

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    tx: float = 0.0
    ty: float = 0.0

    @classmethod
    def translate(cls, x: float, y: float) -> XForm:
        """Pure translation to (x, y). Identity rotation and scale."""
        return cls(tx=x, ty=y)

    @classmethod
    def rotate(cls, angle_deg: float, tx: float = 0.0, ty: float = 0.0) -> XForm:
        """Rotation by angle_deg degrees CCW, then translate to (tx, ty)."""
        rad = math.radians(angle_deg)
        return cls(a=math.cos(rad), b=math.sin(rad), c=-math.sin(rad), d=math.cos(rad), tx=tx, ty=ty)

    @classmethod
    def rotate90ccw(cls, tx: float = 0.0, ty: float = 0.0) -> XForm:
        """90° CCW rotation + translation.

        Text with this transform reads bottom-to-top — the standard
        orientation for Y-axis labels.
        """
        return cls(a=0.0, b=1.0, c=-1.0, d=0.0, tx=tx, ty=ty)

    def to_str(self) -> str:
        """Render as a space-separated XForm attribute string."""

        def _f(v: float) -> str:
            # Compact representation: prefer integers for clean values
            if v == 0.0:
                return "0"
            if v == 1.0:
                return "1"
            if v == -1.0:
                return "-1"
            return f"{v:.6g}"

        return f"{_f(self.a)} {_f(self.b)} {_f(self.c)} {_f(self.d)} {_f(self.tx)} {_f(self.ty)}"


# ── CutSetting ────────────────────────────────────────────────────────────────


@dataclass
class CutSetting:
    """Laser layer parameters.

    Corresponds to one <CutSetting> element. Every shape on the layer
    references this by its index via the CutIndex attribute.

    All cut-mode parameters are included so the generated files open cleanly
    in any version of LightBurn. Engrave-only parameters (interval, bidir,
    crosshatch) are stored with defaults and have no effect in Cut mode.
    """

    index: int
    name: str
    mode: CutMode = CutMode.CUT
    min_power: float = 20.0  # %
    max_power: float = 80.0  # %
    speed: float = 100.0  # mm/s
    kerf: float = 0.0  # mm, kerf compensation offset
    z_offset: float = 0.0  # mm, initial Z offset for this layer
    z_per_pass: float = 0.0  # mm, Z step per pass (negative = deeper into material)
    num_passes: int = 1
    do_output: bool = True
    # Engrave-mode settings (harmless in Cut mode)
    interval: float = 0.1  # mm, line interval for Scan mode
    bidir: bool = True
    crosshatch: bool = False

    def to_element(self) -> ET.Element:
        """Build a <CutSetting type="…"> Element with all child fields."""
        el = ET.Element("CutSetting", type=str(self.mode))

        def f(name: str, value: float | int | str) -> None:
            ET.SubElement(el, name, Value=str(value))

        f("index", self.index)
        f("name", self.name)
        f("minPower", self.min_power)
        f("maxPower", self.max_power)
        f("minPower2", 0)
        f("maxPower2", 0)
        f("speed", self.speed)
        f("kerf", self.kerf)
        f("zOffset", self.z_offset)
        f("enableLaser1", 1)
        f("enableLaser2", 0)
        f("startDelay", 0)
        f("endDelay", 0)
        f("throughPower", 0)
        f("throughPower2", 0)
        f("enableCutThroughStart", 0)
        f("enableCutThroughEnd", 0)
        f("priority", self.index)
        f("frequency", 20000)
        f("overrideFrequency", 0)
        f("PPI", 200)
        f("enablePPI", 0)
        f("doOutput", 1 if self.do_output else 0)
        f("hide", 0)
        f("runBlower", 1)
        f("autoBlower", 0)
        f("blowerSpeedOverride", 0)
        f("blowerSpeedPercent", 100)
        f("overcut", 0)
        f("rampLength", 0)
        f("rampOuter", 0)
        f("numPasses", self.num_passes)
        f("zPerPass", self.z_per_pass)
        f("perforate", 0)
        f("perfLen", 0.1)
        f("perfSkip", 0.1)
        f("dotMode", 0)
        f("dotTime", 1)
        f("dotSpacing", 0.1)
        f("manualTabs", 1)
        f("tabSize", 0.5)
        f("tabCount", 1)
        f("tabSpacing", 50)
        f("skipInnerTabs", 0)
        f("tabsUseSpacing", 1)
        f("scanOpt", "mergeAll")
        f("bidir", 1 if self.bidir else 0)
        f("crossHatch", 1 if self.crosshatch else 0)
        f("overscan", 0)
        f("overscanPercent", 2.5)
        f("floodFill", 0)
        f("interval", self.interval)
        f("angle", 0)
        f("cellsPerInch", 50)
        f("halftoneAngle", 22.5)
        return el


# ── Shapes ────────────────────────────────────────────────────────────────────


@dataclass
class RectShape:
    """Axis-aligned rectangle.

    xform.tx/ty is the CENTRE of the rectangle.
    """

    cut_index: int
    width: float  # mm
    height: float  # mm
    xform: XForm
    corner_radius: float = 0.0  # mm

    def to_element(self) -> ET.Element:
        el = ET.Element(
            "Shape",
            Type="Rect",
            CutIndex=str(self.cut_index),
            W=f"{self.width:g}",
            H=f"{self.height:g}",
            Cr=f"{self.corner_radius:g}",
        )
        ET.SubElement(el, "XForm").text = self.xform.to_str()
        return el


@dataclass
class TextShape:
    """Text shape.

    xform.tx/ty is the anchor point. ah/av select which edge/corner of the
    text bounding box the anchor maps to.

    For rotated text (e.g. Y-axis labels), use XForm.rotate90ccw() as the
    xform — the text will read bottom-to-top.
    """

    cut_index: int
    text: str
    height: float  # mm (approximate cap-height)
    xform: XForm
    font: str = "Arial"
    bold: bool = False
    ah: HAlign = HAlign.LEFT
    av: VAlign = VAlign.TOP

    def _font_str(self) -> str:
        """Qt QFont::toString() format used by LightBurn."""
        weight = 75 if self.bold else 50
        return f"{self.font},-1,100,5,{weight},0,0,0,0,0"

    def to_element(self) -> ET.Element:
        el = ET.Element(
            "Shape",
            Type="Text",
            CutIndex=str(self.cut_index),
            Font=self._font_str(),
            Str=self.text,
            H=f"{self.height:g}",
            LS="0",
            LnS="0",
            Ah=str(self.ah),
            Av=str(self.av),
            Weld="1",
        )
        ET.SubElement(el, "XForm").text = self.xform.to_str()
        return el


#: Union of all supported shape types (for type annotations).
AnyShape = RectShape | TextShape


# ── LightBurnProject ──────────────────────────────────────────────────────────

_UIPREFS: list[tuple[str, int]] = [
    ("Optimize_ByLayer", 0),
    ("Optimize_ByGroup", -1),
    ("Optimize_ByPriority", 1),
    ("Optimize_WhichDirection", 0),
    ("Optimize_InnerToOuter", 1),
    ("Optimize_ByDirection", 0),
    ("Optimize_ReduceTravel", 1),
    ("Optimize_HideBacklash", 0),
    ("Optimize_ReduceDirChanges", 0),
    ("Optimize_ChooseCorners", 0),
    ("Optimize_AllowReverse", 1),
    ("Optimize_RemoveOverlaps", 0),
    ("Optimize_OptimalEntryPoint", 1),
]


@dataclass
class LightBurnProject:
    """Root LightBurn project document.

    Holds an ordered list of cut settings (layers) and shapes.
    Shapes must reference a cut_index matching one of the cut settings.
    """

    cut_settings: list[CutSetting] = field(default_factory=list)
    shapes: list[AnyShape] = field(default_factory=list)
    notes: str = ""
    material_height: float = 0.0
    # MirrorX/MirrorY: machine-specific homing settings saved in the file.
    # "True" for the common diode/galvo setup with home at upper-right.
    # Does not affect file coordinate interpretation.
    mirror_x: bool = True
    mirror_y: bool = True

    def to_element(self) -> ET.Element:
        """Build the complete <LightBurnProject> Element tree."""
        root = ET.Element(
            "LightBurnProject",
            AppVersion=LBRN2_APP_VERSION,
            FormatVersion=LBRN2_FORMAT_VERSION,
            MaterialHeight=f"{self.material_height:g}",
            MirrorX="True" if self.mirror_x else "False",
            MirrorY="True" if self.mirror_y else "False",
        )

        # VariableText — required boilerplate
        vt = ET.SubElement(root, "VariableText")
        for name, val in [("Start", 0), ("End", 999), ("Current", 0), ("Increment", 1), ("AutoAdvance", 0)]:
            ET.SubElement(vt, name, Value=str(val))

        # UIPrefs — optimiser defaults
        prefs = ET.SubElement(root, "UIPrefs")
        for name, val in _UIPREFS:
            ET.SubElement(prefs, name, Value=str(val))

        for cs in self.cut_settings:
            root.append(cs.to_element())

        for shape in self.shapes:
            root.append(shape.to_element())

        if self.notes:
            ET.SubElement(root, "Notes", ShowOnLoad="0", Notes=self.notes)

        return root

    def to_xml_str(self) -> str:
        """Serialise to a well-indented UTF-8 XML string with declaration."""
        root = self.to_element()
        ET.indent(root, space="    ")
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
