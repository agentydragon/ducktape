#!/usr/bin/env bash
# Download and extract the FreeCAD AppImage to /tmp/freecad-explore/squashfs-root.
# Safe to re-run: skips download/extract if already done.
set -euo pipefail

APPIMAGE_URL="https://github.com/FreeCAD/FreeCAD/releases/download/1.1.0/FreeCAD_1.1.0-Linux-x86_64-py311.AppImage"
DIR=/tmp/freecad-explore
APPIMAGE="$DIR/FreeCAD.AppImage"
SQUASHFS="$DIR/squashfs-root"

mkdir -p "$DIR"

if [ ! -f "$APPIMAGE" ]; then
  echo "Downloading FreeCAD AppImage (~1GB)..."
  wget -q --show-progress "$APPIMAGE_URL" -O "$APPIMAGE"
fi

if [ ! -d "$SQUASHFS" ]; then
  echo "Extracting AppImage..."
  chmod +x "$APPIMAGE"
  cd "$DIR" && "$APPIMAGE" --appimage-extract
  echo "Extracted to $SQUASHFS"
fi

echo "FreeCAD ready at $SQUASHFS"
echo "freecadcmd: $SQUASHFS/usr/bin/freecadcmd"
