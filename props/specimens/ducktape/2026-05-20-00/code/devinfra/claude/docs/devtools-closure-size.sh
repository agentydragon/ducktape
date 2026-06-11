#!/usr/bin/env bash
# Print the .#devtools Nix closure breakdown sorted by NAR size.
# Usage: ./devtools-closure-size.sh [flake-ref]
set -euo pipefail

FLAKE_REF="${1:-.#devtools}"
STORE_PATH=$(nix path-info "$FLAKE_REF" 2>/dev/null)

echo "Closure for: $FLAKE_REF"
echo "Store path:  $STORE_PATH"
echo

nix-store -qR "$STORE_PATH" | while read -r p; do
  size=$(nix-store -q --size "$p")
  echo "$size $p"
done | sort -rn | python3 -c "
import sys

lines = []
total = 0
for line in sys.stdin:
    size_str, path = line.strip().split(' ', 1)
    size = int(size_str)
    total += size
    # Strip /nix/store/<hash>- prefix for readability
    name = path.split('-', 1)[1] if '-' in path else path
    lines.append((size, name, path))

cumul = 0
print(f'| {\"Size\":>10} | {\"Cumul%\":>6} | Store path')
print(f'|{\"\":-^12}|{\"\":-^8}|{\"\":-^60}')
for size, name, path in lines:
    cumul += size
    print(f'| {size/1024/1024:8.1f} MiB | {cumul*100/total:5.1f}% | {name}')
print()
print(f'Total: {total/1024/1024:.1f} MiB across {len(lines)} store paths')
"
