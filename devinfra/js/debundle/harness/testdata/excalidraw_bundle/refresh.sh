#!/usr/bin/env bash
# Refresh the Excalidraw debundler benchmark fixture from excalidraw.com.
# Hashes change on every deploy, so this fetches the index.html, parses the
# entry/preload chunk URLs, and downloads them. Update PROVENANCE.md +
# js-files.txt manually after running.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

html=$(curl -sSL https://excalidraw.com)
entry=$(printf '%s' "$html" | grep -oE 'src="/assets/[A-Za-z0-9_.-]+\.js"' | head -1 | sed -E 's|src="/assets/||;s|"$||')
preload=$(printf '%s' "$html" | grep -oE 'modulepreload" crossorigin href="/assets/[A-Za-z0-9_.-]+\.js"' | head -1 | sed -E 's|.*assets/||;s|"$||')
version=$(printf '%s' "$html" | grep -oE 'name="version" content="[^"]*"' | head -1 | sed -E 's|.*content="||;s|"$||')

echo "deploy version: ${version:-unknown}"
echo "entry: $entry"
echo "preload: $preload"

rm -rf static
mkdir -p static
(cd static && curl -sSL -O "https://excalidraw.com/assets/$entry" -O "https://excalidraw.com/assets/$preload")

cat >js-files.txt <<EOF
static/$entry
static/$preload
EOF

ls -lh static/
echo
echo "Now update PROVENANCE.md (deploy version, file table, fetched date)."
