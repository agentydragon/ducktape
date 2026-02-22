# LightBurn `.lbrn2` Format Research

## Sources

- [LightBurn forum — LBRN2 File Documentation](https://forum.lightburnsoftware.com/t/lbrn2-file-documentation/52174)
- [LightBurn forum — LBRN or LBRN2 XML file documentation](https://forum.lightburnsoftware.com/t/lbrn-or-lbrn2-xml-file-docuumentation/42317)
- [nuCarve — LightBurn LBRN vs. LBRN2 file formats](https://nucarve.com/lightburn-file-format-lbrn-lbrn2/)
- [nuCarve — Documenting the LightBurn file format: what's in a shape](https://nucarve.com/lightburn-file-format-whats-in-a-shape/)
- [GitHub — MarcinZukowski/lightburn-tester](https://github.com/MarcinZukowski/lightburn-tester) — cloned to `refs/lightburn-tester/` for detailed study
- [GitHub — jlucaso1/lbrn2-to-svg](https://github.com/jlucaso1/lbrn2-to-svg) — TypeScript lbrn2 parser, cloned to `refs/lbrn2-to-svg/`
- [GitHub — makerspace-gt/lightburn-settings](https://github.com/makerspace-gt/lightburn-settings) — real-world `.lbrn` example files

## Format Overview

LightBurn saves projects as XML in two related formats:

| Format  | Extension | `FormatVersion` | Notes                                                 |
| ------- | --------- | --------------- | ----------------------------------------------------- |
| Legacy  | `.lbrn`   | `"0"`           | Path shapes use verbose `<V>`/`<P>` children          |
| Current | `.lbrn2`  | `"1"`           | Path shapes use compact `VertList`/`PrimList` strings |

**Key fact**: `Rect` and `Text` shape elements are **identical** between `.lbrn` and `.lbrn2`.
Only `Path` shapes differ. Since we only use `Rect` and `Text`, both formats are equivalent for
our purposes. We target `.lbrn2` (`FormatVersion="1"`) as it is the current default.

The schema is officially undocumented by LightBurn Software. All knowledge below comes from
reverse-engineering example files and community resources.

## File Structure

```xml
<?xml version="1.0" encoding="UTF-8"?>
<LightBurnProject AppVersion="1.6.00" FormatVersion="1"
                  MaterialHeight="0" MirrorX="True" MirrorY="True">

  <Thumbnail Source="...base64 PNG..."/>

  <VariableText>
    <Start Value="0"/>
    <End Value="999"/>
    <Current Value="0"/>
    <Increment Value="1"/>
    <AutoAdvance Value="0"/>
  </VariableText>

  <UIPrefs>
    <Optimize_ByLayer Value="0"/>
    <Optimize_ByGroup Value="-1"/>
    <Optimize_ByPriority Value="1"/>
    <Optimize_InnerToOuter Value="1"/>
    <!-- ... more optimizer prefs ... -->
  </UIPrefs>

  <!-- One or more CutSetting elements (layers) -->
  <CutSetting type="Cut">
    <index Value="0"/>
    <name Value="C00"/>
    <!-- ... per-setting child elements ... -->
  </CutSetting>

  <!-- Shape elements (any order) -->
  <Shape Type="Rect" CutIndex="0" W="15" H="15" Cr="0">
    <XForm>1 0 0 1 50 30</XForm>
  </Shape>

  <Shape Type="Text" CutIndex="0" Font="Arial,-1,100,5,50,0,0,0,0,0"
         Str="Hello" H="8" LS="0" LnS="0" Ah="1" Av="1" Weld="1">
    <XForm>1 0 0 1 50 10</XForm>
  </Shape>

  <Notes ShowOnLoad="0" Notes="Optional notes text"/>

</LightBurnProject>
```

### Root element attributes

| Attribute        | Example    | Notes                                      |
| ---------------- | ---------- | ------------------------------------------ |
| `AppVersion`     | `"1.6.00"` | LightBurn version that created the file    |
| `FormatVersion`  | `"1"`      | `"0"` = lbrn legacy; `"1"` = lbrn2 current |
| `MaterialHeight` | `"0"`      | Material height (mm) for Z focus offset    |
| `MirrorX`        | `"True"`   | Machine X-axis mirror setting              |
| `MirrorY`        | `"True"`   | Machine Y-axis mirror setting              |

`MirrorX`/`MirrorY` are machine-specific settings saved in the file. They reflect the homing
position of the laser head and do not change how coordinates are interpreted in the file.

## Coordinate System

- Origin `(0, 0)` is at the **top-left** of the workspace as displayed in LightBurn.
- **X** increases to the right.
- **Y** increases **downward** (screen/document convention, not math convention).
- All units are **millimetres**.
- Shapes are positioned with an affine `XForm` matrix.

## XForm (Transformation Matrix)

All shapes have an `<XForm>` child element. The value is six space-separated floats:

```
<XForm>a b c d e f</XForm>
```

This represents a 2D affine transform:

```
x' = a*x + c*y + e
y' = b*x + d*y + f
```

| Transform                        | a   | b   | c   | d   | e   | f   |
| -------------------------------- | --- | --- | --- | --- | --- | --- |
| Identity (no rotation, no scale) | 1   | 0   | 0   | 1   | 0   | 0   |
| Translate to (tx, ty)            | 1   | 0   | 0   | 1   | tx  | ty  |
| Rotate 90° CCW around origin     | 0   | 1   | −1  | 0   | 0   | 0   |
| Rotate 90° CW around origin      | 0   | −1  | 1   | 0   | 0   | 0   |

**Rect**: `(e, f)` is the **centre** of the rectangle.

**Text**: `(e, f)` is the **anchor point** of the text, with `Ah`/`Av` controlling which corner/edge
of the text bounding box the anchor maps to (see Text section below).

## CutSetting Element

Each `<CutSetting>` defines one laser layer (colour). Shapes reference their layer via `CutIndex`.

```xml
<CutSetting type="Cut">
  <index Value="0"/>
  <name Value="C00"/>
  <minPower Value="20"/>
  <maxPower Value="80"/>
  <minPower2 Value="0"/>
  <maxPower2 Value="0"/>
  <speed Value="100"/>
  <kerf Value="0"/>
  <zOffset Value="0"/>
  <numPasses Value="1"/>
  <zPerPass Value="0"/>
  <enableLaser1 Value="1"/>
  <enableLaser2 Value="0"/>
  <startDelay Value="0"/>
  <endDelay Value="0"/>
  <throughPower Value="0"/>
  <throughPower2 Value="0"/>
  <enableCutThroughStart Value="0"/>
  <enableCutThroughEnd Value="0"/>
  <priority Value="0"/>
  <frequency Value="20000"/>
  <overrideFrequency Value="0"/>
  <PPI Value="200"/>
  <enablePPI Value="0"/>
  <doOutput Value="1"/>
  <hide Value="0"/>
  <runBlower Value="1"/>
  <autoBlower Value="0"/>
  <blowerSpeedOverride Value="0"/>
  <blowerSpeedPercent Value="100"/>
  <overcut Value="0"/>
  <rampLength Value="0"/>
  <rampOuter Value="0"/>
  <perforate Value="0"/>
  <perfLen Value="0.1"/>
  <perfSkip Value="0.1"/>
  <dotMode Value="0"/>
  <dotTime Value="1"/>
  <dotSpacing Value="0.1"/>
  <manualTabs Value="1"/>
  <tabSize Value="0.5"/>
  <tabCount Value="1"/>
  <tabSpacing Value="50"/>
  <skipInnerTabs Value="0"/>
  <tabsUseSpacing Value="1"/>
  <scanOpt Value="mergeAll"/>
  <bidir Value="1"/>
  <crossHatch Value="0"/>
  <overscan Value="0"/>
  <overscanPercent Value="2.5"/>
  <floodFill Value="0"/>
  <interval Value="0.1"/>
  <angle Value="0"/>
  <cellsPerInch Value="50"/>
  <halftoneAngle Value="22.5"/>
</CutSetting>
```

### Key CutSetting attributes

| XML element  | Type         | Description                                                  |
| ------------ | ------------ | ------------------------------------------------------------ |
| `index`      | int          | 0-based layer index; must match the `CutIndex` on shapes     |
| `name`       | string       | Layer name shown in LightBurn UI (e.g. `"C00"`)              |
| `type` attr  | string       | `"Cut"` (line), `"Scan"` (fill/engrave), `"Scan+Cut"` (both) |
| `minPower`   | float (%)    | Minimum laser power                                          |
| `maxPower`   | float (%)    | Maximum laser power                                          |
| `speed`      | float (mm/s) | Travel speed                                                 |
| `kerf`       | float (mm)   | Kerf offset; positive = expand outward, negative = shrink    |
| `zOffset`    | float (mm)   | Initial Z offset applied before cutting this layer           |
| `numPasses`  | int          | Number of passes over each shape                             |
| `zPerPass`   | float (mm)   | Z axis step per pass; negative = move deeper                 |
| `doOutput`   | 0/1          | Whether LightBurn will output this layer                     |
| `hide`       | 0/1          | Whether layer is hidden in UI                                |
| `interval`   | float (mm)   | Line interval for Scan/fill mode                             |
| `crossHatch` | 0/1          | Crosshatch scan mode                                         |
| `bidir`      | 0/1          | Bidirectional scanning (engrave mode)                        |

Layer index 0 is conventionally used for text/annotations at low power.

## Shape: Rect

```xml
<Shape Type="Rect" CutIndex="0" W="15" H="15" Cr="0">
  <XForm>1 0 0 1 50 30</XForm>
</Shape>
```

| Attribute     | Description                                   |
| ------------- | --------------------------------------------- |
| `W`           | Width (mm)                                    |
| `H`           | Height (mm)                                   |
| `Cr`          | Corner radius (mm); `0` = sharp corners       |
| `CutIndex`    | Layer index (must match a `CutSetting index`) |
| `XForm (e,f)` | **Centre** of the rectangle                   |

## Shape: Text

```xml
<Shape Type="Text" CutIndex="0"
       Font="Arial,-1,100,5,50,0,0,0,0,0"
       Str="Hello world" H="8"
       LS="0" LnS="0" Ah="1" Av="1" Weld="1">
  <XForm>1 0 0 1 50 10</XForm>
</Shape>
```

| Attribute     | Description                                           |
| ------------- | ----------------------------------------------------- |
| `Font`        | Font descriptor (see below)                           |
| `Str`         | Text content (XML-escaped)                            |
| `H`           | Text height (mm)                                      |
| `LS`          | Letter spacing (0 = default)                          |
| `LnS`         | Line spacing (0 = default)                            |
| `Ah`          | Horizontal alignment: `0`=left, `1`=centre, `2`=right |
| `Av`          | Vertical alignment: `0`=top, `1`=centre, `2`=bottom   |
| `Weld`        | Weld overlapping characters: `1`=yes                  |
| `CutIndex`    | Layer index                                           |
| `XForm (e,f)` | Anchor point (interpretation depends on `Ah`/`Av`)    |

### Font descriptor string

Format: `"family,-1,pixelSize,styleHint,weight,italic,underline,strikeOut,fixedPitch,kerning"`

This is Qt's `QFont::toString()` format. LightBurn uses the `H` attribute for actual rendered
height in mm; the `pixelSize` field in the font string is largely ignored.

| Field        | Typical value | Notes                                   |
| ------------ | ------------- | --------------------------------------- |
| `family`     | `Arial`       | Font family name                        |
| `-1`         | `-1`          | Point size (−1 = use pixel size)        |
| `pixelSize`  | `100`         | Pixel size (overridden by `H` in mm)    |
| `styleHint`  | `5`           | Qt style hint enum (5 = AnyStyle)       |
| `weight`     | `50`          | Qt font weight (50 = Normal, 75 = Bold) |
| `italic`     | `0`           | 0/1                                     |
| `underline`  | `0`           | 0/1                                     |
| `strikeOut`  | `0`           | 0/1                                     |
| `fixedPitch` | `0`           | 0/1                                     |
| `kerning`    | `0`           | 0/1                                     |

Examples:

- Normal Arial: `"Arial,-1,100,5,50,0,0,0,0,0"`
- Bold Arial: `"Arial,-1,100,5,75,0,0,0,0,0"`

### Text rotation

Rotate a text label 90° CCW (reads bottom-to-top, standard Y-axis label orientation):

```xml
<Shape Type="Text" ... Ah="1" Av="1">
  <XForm>0 1 -1 0 10 80</XForm>   <!-- 90° CCW, centred at (10, 80) -->
</Shape>
```

## Shape: Path (for reference — not used in this project)

`.lbrn` (legacy) format:

```xml
<Shape Type="Path" CutIndex="0">
  <XForm>1 0 0 1 0 0</XForm>
  <V vx="10" vy="0" c0x="10" c0y="-5" c1x="10" c1y="5"/>
  <V vx="20" vy="10"/>
  <P T="L" p0="0" p1="1"/>
  <P T="B" p0="1" p1="2"/>
</Shape>
```

`.lbrn2` (current) format — same data, compact encoding:

```xml
<Shape Type="Path" CutIndex="0" VertID="0" PrimID="0">
  <XForm>1 0 0 1 0 0</XForm>
  <VertList>V10 0c0x10c0y-5c1x10c1y5V20 10</VertList>
  <PrimList>L0 1B1 2</PrimList>
</Shape>
```

## Existing Open-Source Implementations

| Project                                                                | Language   | Read     | Write   | Notes                                                                                |
| ---------------------------------------------------------------------- | ---------- | -------- | ------- | ------------------------------------------------------------------------------------ |
| [lightburn-tester](https://github.com/MarcinZukowski/lightburn-tester) | Python     | —        | `.lbrn` | Parametric test grid generator (fill/engrave focus); uses template prologue/epilogue |
| [lbrn2-to-svg](https://github.com/jlucaso1/lbrn2-to-svg)               | TypeScript | `.lbrn2` | —       | Reads lbrn2, converts to SVG; handles Rect, Ellipse, Path, Text, Bitmap              |

No existing Python writer for `.lbrn2` with Z-offset / z-per-pass / kerf support was found.
The `lightburn-tester` project is the closest match and informed this implementation.
