# FreeCAD AppImage exploration

Proof-of-concept: run FreeCAD directly from its AppImage (no Docker container)
and produce a DXF file.

## Result

Works. `rect.py` creates a 100×50 rectangle and exports `rect.dxf` + `rect.FCStd`
in ~2s, no container, no xvfb.

## Setup

```bash
./setup.sh   # downloads AppImage (~783MB) and extracts to /tmp/freecad-explore/squashfs-root
```

## Run

```bash
./run.sh              # runs rect.py, outputs to /tmp/freecad-explore/out/
./run.sh my_script.py # run a different script
OUTDIR=/tmp/foo ./run.sh
```

## Key findings

### Environment variables required

```bash
export SQUASHFS=/tmp/freecad-explore/squashfs-root
export PREFIX="$SQUASHFS/usr"
export PYTHONHOME="$SQUASHFS/usr"            # FreeCAD's bundled Python
export PATH_TO_FREECAD_LIBDIR="$SQUASHFS/usr/lib"
export SSL_CERT_FILE="$SQUASHFS/usr/ssl/cacert.pem"
export PATH="$SQUASHFS/usr/bin:$PATH"
export QT_QPA_PLATFORM=offscreen             # no display needed for Part ops
```

### DXF export: use `importDXF`, not `Part.export`

`Part.export([feat], "out.dxf")` fails with `Unknown extension` in headless mode.
`importDXF.export([feat], "out.dxf")` works correctly (produces LINE entities).

### xvfb not needed for Part/importDXF

`QT_QPA_PLATFORM=offscreen` is sufficient for scripts that only use Part geometry
and `importDXF`. Scripts that use TechDraw (HLR rendering, `FreeCADGui`) still
need xvfb.

### AppImage extraction

`./FreeCAD.AppImage --appimage-extract` works without FUSE (user-mode extraction).
No root needed. Takes ~10s, produces ~2.8GB in `squashfs-root/`.

## Next steps toward a Bazel repository rule

1. Write a `repository_rule` that downloads the AppImage via `http_file` and
   extracts it with `--appimage-extract`. Expose `squashfs-root/` as a filegroup.
2. In tests: replace `LoggedContainer` + Docker exec with direct `subprocess.run`
   against `$SQUASHFS/usr/bin/freecadcmd`, passing the env vars above.
3. For TechDraw tests (HLR rendering): add `xvfb-run` wrapper and ensure the
   RBE worker image has `xvfb` (it already has it for the container tests).
4. Font determinism: the AppImage bundles osifont at
   `squashfs-root/usr/Mod/TechDraw/Resources/fonts/`. Need to register it with
   fontconfig via `FONTCONFIG_FILE` pointing to a generated config, or `fc-cache`
   at test startup.
