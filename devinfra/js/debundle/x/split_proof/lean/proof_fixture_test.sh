#!/usr/bin/env bash
set -euo pipefail
render=$(rlocation "$1")
node=$(rlocation "$2")
bundle=$(rlocation "$3")
dir="$TEST_TMPDIR/proof_fixture"
mkdir -p "$dir/good" "$dir/bad" "$dir/forced"
"$render" "$dir/good"
cp "$bundle" "$dir/bundle.mjs"
# Test-only exports let Node observe local bindings without changing the input fixture.
printf '\nexport { unused, one, copy };\n' >> "$dir/bundle.mjs"
printf '\nexport { unused, copy };\n' >> "$dir/good/main.mjs"
"$node" --input-type=module - "$dir/bundle.mjs" "$dir/good/main.mjs" <<'JS'
import { pathToFileURL } from "node:url";
import { deepStrictEqual } from "node:assert";
const before = await import(pathToFileURL(process.argv[2]));
const after = await import(pathToFileURL(process.argv[3]));
deepStrictEqual([after.unused, after.copy], [before.unused, before.copy]);
JS
if "$render" "$dir/bad" --bad; then
  echo 'unsafe split was accepted' >&2
  exit 1
fi
[[ ! -e "$dir/bad/main.mjs" ]]
if "$render" "$dir/forced" --bad --force; then
  echo 'forced unsafe split returned success' >&2
  exit 1
fi
if "$node" "$dir/forced/main.mjs" >"$dir/node.log" 2>&1; then
  echo 'unsafe split unexpectedly executed successfully' >&2
  exit 1
fi
grep -q 'ReferenceError' "$dir/node.log"
