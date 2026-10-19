#!/usr/bin/env bash
set -euo pipefail

bun_binary="$(realpath "$1")"
cd "$(dirname "$2")"
exec "$bun_binary" --preserve-symlinks test
