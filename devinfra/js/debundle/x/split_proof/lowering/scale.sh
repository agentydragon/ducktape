#!/usr/bin/env bash
# Measure the diagnostic checker on contiguous block splits. See experiments/README.md § 5.
set -euo pipefail

cd "$(dirname "$0")"
bundles=${BUNDLES_DIR:-bundles}
split_preview=${SPLIT_PREVIEW:-../lean/.lake/build/bin/split_preview}
work=$(mktemp -d "${T:-${TMPDIR:-/tmp}}/split-proof-scale.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
failed=0

row() {
  local label=$1 bundle=$2 blocks=$3 copies=${4:-1}
  local dir dimensions statements modules deployment status=0 verdict
  dir=$(mktemp -d "$work/row.XXXXXX")
  dimensions=$(
    node --input-type=module - "$bundle" "$blocks" "$copies" "$dir/spec.json" <<'JS'
import { readFileSync, writeFileSync } from "node:fs";
import { spec } from "../integration/library_splits/gen_spec.mjs";
import { topLevelStatements } from "../translation/scope_metadata.mjs";
import { writeDeployment } from "../translation/testing/deployment_manifest.mjs";
import path from "node:path";

const [bundle, blocks, copies, output] = process.argv.slice(2);
const program = JSON.parse(readFileSync(bundle, "utf8"));
const statements = topLevelStatements(program).length * Number(copies);
const placement = spec(statements, "blocks", [blocks]);
writeFileSync(output, JSON.stringify(placement));
console.log(statements, Object.keys(placement.modules).length, writeDeployment(bundle, output, path.join(path.dirname(output), "out")));
JS
  )
  read -r statements modules deployment <<<"$dimensions"

  local -a command=("$split_preview" "$deployment" "$dir/out")
  local rejection="rejected; nothing written"
  if [[ $# -eq 4 ]]; then
    command=("${REPLICATE_PLAN:-../lean/.lake/build/bin/replicate_plan}" "$bundle" "$dir/spec.json" "$copies")
    rejection="planner rejected"
  fi
  timeout 240 "${command[@]}" >"$dir/log" 2>&1 || status=$?
  case "$status" in
    0) verdict=accepted ;;
    124) verdict="timed out" ;;
    *)
      if [[ $status -eq 1 ]] && grep -Fxq "$rejection" "$dir/log"; then
        verdict=rejected
      else
        verdict="error (exit $status)"
        cat "$dir/log" >&2
        failed=1
      fi
      ;;
  esac
  awk -v label="$label" -v n="$statements" -v k="$modules" -v verdict="$verdict" '
    /\[time\] read/ { json = $(NF - 1) }
    /\[time\] footprints/ { fp = $(NF - 1) }
    /\[time\] mkPlan/ { plan = $(NF - 1) }
    /^ +(pass|FAIL) / && /ms\)/ { t[$2] = substr($3, 2); if ($1 == "FAIL") bad = bad " " $2; next }
    /^ +FAIL/ { bad = bad " front-end rule" }
    /\[time\] all checks/ { checks = $(NF - 1) }
    END { printf "| %s | %d | %d | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |\n", label, n, k, json, fp, plan, t["UniqueDefs"], t["ReadsClosed"], t["Faithful"], t["Linkable"], t["Hygienic"], t["Permutes"], t["OrderOk"], checks, verdict (verdict == "rejected" ? ":" bad : "") }' "$dir/log"
  rm -rf -- "$dir"
}

echo "| Input | Statements | Modules | JSON | Footprints | mkPlan | UniqueDefs | ReadsClosed | Faithful | Linkable | Hygienic | Permutes | OrderOk | All checks | Verdict |"
echo "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
if [[ ${1:-} == replicate ]]; then
  for copies in 1 2 4 8 16 32 64; do
    row "lodash x$copies" "$bundles/lodash.json" $((6 * copies)) "$copies"
  done
else
  for n in 100 200 300; do
    node prefix.mjs "$bundles/lodash.json" "$n" >"$work/lodash_prefix.json"
    row "lodash, first $n" "$work/lodash_prefix.json" $((n / 25))
  done
  row "semver" "$bundles/semver.json" 4
  row "lodash" "$bundles/lodash.json" 6
  row "acorn" "$bundles/acorn.json" 5
  row "acorn, minified" "$bundles/acorn.min.json" 5
  row "zod, minified" "$bundles/zod.min.json" 8
  row "zod" "$bundles/zod.json" 8
fi
exit "$failed"
