---
name: ikea-3d-models
description: >-
  Find an IKEA product and produce a correctly-scaled STL mesh ready for
  FreeCAD (search → GLB download → Draco-decompress → STL; also converts a
  bare .glb). Use when the user wants an IKEA item as a 3D/CAD model or a
  .glb turned into an STL.
---

# IKEA 3D models → STL

Turn an IKEA product (by name or item number) into an STL mesh you can import
into FreeCAD (File → Import). The whole pipeline needs **no IKEA login and no
cookies**, and the convert step needs **no FreeCAD** (pure Python).

## Pipeline

```
product name ──search_ikea.py──▶ item number
item number ──download_ikea_glb.sh──▶ product.glb   (Draco-compressed)
product.glb ──glb_to_stl.sh──▶ product.stl
```

Scripts live in `scripts/` next to this file. Make them executable once
(`chmod +x scripts/*.sh scripts/*.py`).

### 1. Find the item number

```bash
scripts/search_ikea.py "besta tv unit"
# 70474062  BESTÅ TV unit  (70 7/8x15 3/4x15 ")
# 80299874  BESTÅ TV unit  (70 7/8x15 3/4x25 1/4 ")
# ...
```

This calls IKEA's structured `sik` search backend (JSON) — prefer it over
scraping the JS-rendered search page, which does not reliably contain item
numbers. An "art number" on the label (e.g. `005.660.36`) is the item number
with the dots removed (`00566036`). Add `--json` for machine-readable output,
`--size N` for more results, `--market gb --lang en` for other locales.

If the user gives an exact art/item number, **search the number itself** to
confirm what it is before downloading — `search_ikea.py "00245842"` returns the
single matching product with its name and dimensions:

```bash
scripts/search_ikea.py "00245842"
# 00245842  BESTÅ Frame  (23 5/8x15 3/4x75 5/8 ")
```

Pick the row whose name **and dimensions** match what the user wants (a query
returns many sizes/variants). Confirm with the user if ambiguous.

For a specific color, add `--variants` — it lists every color of each product
with its item number (read structurally from the search response, not scraped):

```bash
scripts/search_ikea.py "besta tv unit" --variants
# 70474062  BESTÅ TV unit  (70 7/8x15 3/4x15 ")
#     40575227  dark gray
#     80566037  white
```

**The exact color often has no model (404).** IKEA only 3D-scans some covers of
a given product. This is usually fine: the output is a geometry-only mesh (no
textures), and **all covers of one product share identical geometry** — so any
scanned variant's item number gives the correct shape and dimensions. When the
user's exact item 404s, HEAD-check its siblings (`--variants`) and use whichever
one has a model; tell the user you used a different cover of the same product.
Note search "snaps" an item-number query to a representative variant, so the
number it echoes back may differ from the one queried — that's expected.

### 2. Download the GLB

```bash
scripts/download_ikea_glb.sh 80299874 besta-tv.glb
```

Downloads the public "rotera" model. It HEAD-checks first and fails clearly if
no model exists (HTTP 404 = discontinued or never scanned — try a different
color variant's item number from `search_ikea.py --variants`). ~270 KB–700 KB
typical.

### 3. Convert to STL

```bash
scripts/glb_to_stl.sh besta-tv.glb besta-tv.stl "BESTA TV unit"
# wrote besta-tv.stl
```

In FreeCAD, File → Import the `.stl` into any document. The third argument is the
name written into the STL header (optional). The converter prints the model's
`BBOX_XYZ` in mm so you can sanity-check the size against the catalog dimensions.

## Why each step exists (gotchas)

- **Draco compression is the whole reason the naive path fails.** IKEA GLBs use
  `KHR_draco_mesh_compression`. FreeCAD's own glTF importer cannot read Draco
  buffers, so importing the raw .glb into FreeCAD _silently adds nothing_ — no
  error, the UI just does nothing. `glb_to_stl.sh` decodes Draco first (via
  `gltf-transform`, a JS tool run through npx/pnpm) and emits STL instead.
- **Meters vs millimeters.** glTF is in meters; FreeCAD documents default to mm.
  Without the ×1000 scale the imported mesh is under a millimeter tall at the
  origin — present but microscopic, another way it looks like "nothing
  imported". `glb_to_stl.py` applies `SCALE=1000` by default.
- **Result is a mesh, not a B-rep solid.** Good for reference, layout, and
  measuring. For real CAD geometry, in FreeCAD use Part → "Create shape from
  mesh" then "Convert to solid" (approximate, lossy for organic shapes).
- **Cookies / auth: not needed.** Search, product pages, and the rotera model
  URL are all public. The only cookie-gated endpoint is the metadata JSON, and
  this skill deliberately avoids it — dimensions come from search or the model's
  own bounding box (printed by the converter as `BBOX_XYZ`, in mm).
- **Higher-detail "dimma" models** exist for some products (e.g. LILLÅNÄS) and
  must be extracted from page JavaScript. See `ikea_api_reference.md`. The
  rotera `-mini` model is the reliable default and is what these scripts use.

## A bare .glb the user already has

Skip steps 1–2 and run step 3 on it. `glb_to_stl.sh` works on any glb (it
decodes Draco if present, passes through if not).

## Environment notes

- Needs Node (`npx`/`pnpm`) for the Draco decode and Python 3 (stdlib only) for
  everything else — `search_ikea.py` and `glb_to_stl.py` need no third-party
  packages and **no FreeCAD**. FreeCAD is only needed by the user, to open the
  resulting STL.
- On a sandboxed Claude Code session, the IKEA hosts and the npm registry are
  not allowlisted and the pnpm store is read-only — run the network/convert
  steps with the sandbox disabled (`/sandbox` to manage).

See `ikea_api_reference.md` for the full IKEA API details (endpoints, variants,
the dimma higher-detail pipeline, market locales).
