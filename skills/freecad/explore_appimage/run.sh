#!/usr/bin/env bash
# Run rect.py using the extracted FreeCAD AppImage.
# Usage: ./run.sh [script.py]   (defaults to rect.py)
set -euo pipefail

SQUASHFS=/tmp/freecad-explore/squashfs-root
SCRIPT="${1:-$(dirname "$0")/rect.py}"
OUTDIR="${OUTDIR:-/tmp/freecad-explore/out}"

if [ ! -d "$SQUASHFS" ]; then
  echo "AppImage not extracted. Run setup.sh first." >&2
  exit 1
fi

mkdir -p "$OUTDIR"

export PREFIX="$SQUASHFS/usr"
export PYTHONHOME="$SQUASHFS/usr"
export PATH_TO_FREECAD_LIBDIR="$SQUASHFS/usr/lib"
export SSL_CERT_FILE="$SQUASHFS/usr/ssl/cacert.pem"
export PATH="$SQUASHFS/usr/bin:$PATH"
export QT_QPA_PLATFORM=offscreen
export OUTDIR

echo "Running: freecadcmd $SCRIPT"
echo "Output:  $OUTDIR"
# QT_QPA_PLATFORM=offscreen is sufficient for Part/importDXF operations.
# Scripts that use TechDraw (FreeCADGui + HLR rendering) still need xvfb:
#   xvfb-run -a -s "-screen 0 1024x768x24" freecadcmd "$SCRIPT"
freecadcmd "$SCRIPT"
echo "Done. Files in $OUTDIR:"
ls -lh "$OUTDIR"
