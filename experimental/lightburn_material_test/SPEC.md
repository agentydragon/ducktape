# Material Test Grid Generator — Feature Specification

Generates parametric material test grids as LightBurn `.lbrn2` files. Each run produces a
grid of laser-cut squares where rows and columns correspond to two varying laser parameters,
making it easy to visually evaluate results across the parameter space in a single job.

## Concept

A rectangular grid of laser-cut squares where:

- Each **column** corresponds to one value of an X-axis parameter
- Each **row** corresponds to one value of a Y-axis parameter
- Each **cell** is cut with that cell's specific (x-param, y-param) combination
- All other parameters are held constant across the entire grid

Example: X axis = power (12%, 13.5%, 15%, 16.5%, 18%), Y axis = ΔZ/pass (−0.5 to −0.8 mm)
→ 20 cells, each cut at a unique (power, z-per-pass) pair.

## Supported Cut Parameters

Parameters that can be varied on X or Y axes, or held constant:

| Parameter name | Description                      | Typical unit |
| -------------- | -------------------------------- | ------------ |
| `power`        | Min and max power (set together) | %            |
| `power_min`    | Minimum power only               | %            |
| `power_max`    | Maximum power only               | %            |
| `speed`        | Cut speed                        | mm/s         |
| `kerf`         | Kerf compensation offset         | mm           |
| `z_offset`     | Z offset at start of layer       | mm           |
| `z_per_pass`   | Z step per pass (typically ≤ 0)  | mm           |
| `num_passes`   | Number of passes                 | integer      |

## Grid Geometry

- `cell_size` — Side length of each square cell, default 15 mm
- `gap` — Gap between adjacent cells (both X and Y), default 8 mm
- Cell stride = `cell_size + gap`
- Grid is tight: no outer padding beyond the cell edges (margin is separate)

## Annotations

### X-axis annotation (above the grid)

When enabled, per-column parameter values appear above each column, centered.
Below the values, the X-axis label appears (centered over all columns).

Example (X = power):

```
12    13.5   15    16.5   18
         Power [%]
```

### Y-axis annotation (left of the grid)

When enabled, per-row parameter values appear to the left of each row, right-aligned
and vertically centered on the row.

To the left of the values, the Y-axis label appears rotated 90° CCW (reading bottom-to-top),
centered vertically over the grid height.

Example (Y = ΔZ/pass):

```
─0.5
─0.6
─0.7   (with "ΔZ/pass [mm]" rotated 90° to the far left)
─0.8
```

### In-cell text (optional)

When enabled, the X and Y parameter values for each cell are printed as two text lines
inside the cell (centered). Font size is configurable and defaults to something small enough
to fit within the cell.

Example for a 15 mm cell with power=15, z_per_pass=−0.6:

```
  15
 -0.6
```

### Title and subtitle (optional)

A title line appears above the grid, and a subtitle appears below the title. The subtitle
can be auto-generated from the constant parameter values.

Example:

```
6.08mm ply, Lauan
Z=−0.1, 15 mm/s, kerf 0.1 mm
```

The auto-generated subtitle includes only parameters that are not being varied on either axis.
An optional extra title text can be prepended to the subtitle (user-supplied).

### Border (optional)

A border rectangle can be drawn around the entire grid, using a separate configurable
cut setting. The border is padded a configurable amount outside the grid cells.

## LightBurn Layer Structure

| Layer index      | Content                                        |
| ---------------- | ---------------------------------------------- |
| 0                | All text (title, labels, values, in-cell text) |
| 1 … N×M          | One cut layer per grid cell (N cols × M rows)  |
| N×M+1 (optional) | Border rectangle layer                         |

Each grid cell gets its own `CutSetting` because the combination of X and Y parameter
values is unique per cell. Layer 0 uses a distinct, low-power cut setting for marking.

## CLI Interface

```
material_test.py \
  --x-param power_max --x-values "12,13.5,15,16.5,18" \
  --y-param z_per_pass --y-values "-0.5,-0.6,-0.7,-0.8" \
  --speed 15 --z-offset -0.1 --kerf 0.1 --num-passes 3 \
  --cell-size 15 --gap 8 \
  --x-label "Power [%]" --y-label "ΔZ/pass [mm]" \
  --title "6.08mm ply, Lauan" \
  --show-cell-text \
  --border --border-power 20 --border-speed 100 \
  --font "Arial" \
  -o material_test.lbrn2
```

### Full parameter reference

**Axis selection:**

| Flag                   | Description                       |
| ---------------------- | --------------------------------- |
| `--x-param NAME`       | Parameter to vary along X axis    |
| `--x-values V1,V2,...` | Comma-separated values for X axis |
| `--y-param NAME`       | Parameter to vary along Y axis    |
| `--y-values V1,V2,...` | Comma-separated values for Y axis |

**Constant cut parameters (defaults apply if omitted):**

| Flag              | Default | Description                       |
| ----------------- | ------- | --------------------------------- |
| `--power PCT`     | 80      | Base power, sets both min and max |
| `--power-min PCT` | —       | Override min power specifically   |
| `--power-max PCT` | —       | Override max power specifically   |
| `--speed MM_S`    | 100     | Cut speed (mm/s)                  |
| `--kerf MM`       | 0       | Kerf compensation (mm)            |
| `--z-offset MM`   | 0       | Initial Z offset (mm)             |
| `--z-per-pass MM` | 0       | Z step per pass (mm)              |
| `--num-passes N`  | 1       | Number of passes                  |

**Geometry:**

| Flag             | Default | Description                  |
| ---------------- | ------- | ---------------------------- |
| `--cell-size MM` | 15      | Square cell side length (mm) |
| `--gap MM`       | 8       | Gap between cells (mm)       |

**Annotations:**

| Flag                 | Default | Description                          |
| -------------------- | ------- | ------------------------------------ |
| `--x-label TEXT`     | `""`    | X-axis label (e.g. `"Power [%]"`)    |
| `--y-label TEXT`     | `""`    | Y-axis label (e.g. `"ΔZ/pass [mm]"`) |
| `--no-x-annotations` | off     | Suppress per-column value labels     |
| `--no-y-annotations` | off     | Suppress per-row value labels        |
| `--show-cell-text`   | off     | Print param values inside each cell  |

**Title:**

| Flag                 | Default | Description                                      |
| -------------------- | ------- | ------------------------------------------------ |
| `--title TEXT`       | `""`    | Main title                                       |
| `--subtitle TEXT`    | `""`    | Extra subtitle text (prepended to auto-subtitle) |
| `--no-auto-subtitle` | off     | Suppress auto-generated constant-param subtitle  |

**Border:**

| Flag                  | Default | Description                     |
| --------------------- | ------- | ------------------------------- |
| `--border`            | off     | Draw border rectangle           |
| `--border-padding MM` | 3       | Padding between grid and border |
| `--border-power PCT`  | 10      | Border layer max power          |
| `--border-speed MM_S` | 200     | Border layer speed              |

**Text cut settings:**

| Flag                | Default | Description                     |
| ------------------- | ------- | ------------------------------- |
| `--text-power PCT`  | 15      | Power for text/annotation layer |
| `--text-speed MM_S` | 200     | Speed for text/annotation layer |

**Font and output:**

| Flag                 | Default               | Description              |
| -------------------- | --------------------- | ------------------------ |
| `--font NAME`        | `Arial`               | Font family for all text |
| `-o / --output FILE` | `material_test.lbrn2` | Output file path         |

## Layout Diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│  Title: "6.08mm ply, Lauan"                                          │
│  Subtitle: "Z=−0.1, 15 mm/s, kerf 0.1 mm"                           │
│                                                                       │
│             12      13.5      15      16.5      18                   │
│                         Power [%]                                    │
│                                                                       │
│        ─0.5  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐     │
│              │ 12   │  │13.5  │  │  15  │  │16.5  │  │  18  │     │
│              │─0.5  │  │─0.5  │  │─0.5  │  │─0.5  │  │─0.5  │     │
│              └──────┘  └──────┘  └──────┘  └──────┘  └──────┘     │
│                                                                       │
│ Δ      ─0.6  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐     │
│ Z            │ ...  │  │ ...  │  │ ...  │  │ ...  │  │ ...  │     │
│ /            └──────┘  └──────┘  └──────┘  └──────┘  └──────┘     │
│ p                                                                     │
│ a      ─0.7  ┌──────┐  ...                                          │
│ s                                                                     │
│ s      ─0.8  ┌──────┐  ...                                          │
│                                                                       │
│  [optional border around grid]                                       │
└──────────────────────────────────────────────────────────────────────┘
```

## Future Work

- **Engrave mode** (Fill/Scan): expose `interval` (line interval mm) and `crosshatch` (bool).
  Show only engrave-relevant params in auto-subtitle.

- **3D / 4D parameter sweep**: nested sub-grid layout, e.g. a grid of grids for scanning
  3 or 4 parameters simultaneously.
