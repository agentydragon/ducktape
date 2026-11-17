#!/bin/bash
set -euo pipefail

hostname
test ! -e /run/current-system/sw/share/nix-ld
binary="$(rlocation "$1")"
env -i "$binary"
