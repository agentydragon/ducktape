#!/usr/bin/env bash
set -euo pipefail

bun_binary="$(realpath "$1")"
cd "$(dirname "$2")"

# Materialize sources so Bun resolves their imports through pnpm's dependency links,
# rather than following runfile symlinks back into the unlinked source archive.
workspace="$TEST_TMPDIR/webui"
mkdir -p "$workspace"
cp -RL src tests package.json tsconfig.json tsconfig.node.json vite.config.ts index.html "$workspace/"
ln -s "$PWD/node_modules" "$workspace/node_modules"
cd "$workspace"
exec "$bun_binary" test
