#!/usr/bin/env bash
# Real bundles through the whole check, compiled. One markdown row per run, every stage in ms.
# The specs are contiguous blocks, a timing workload: most such splits are not valid reshuffles (see § 7 of ../README.md).
# `lowering/bundles/*.json` come from `./run.sh --lowering`. Each run is capped at 240 s. T = scratch directory.
#
#   ./scale.sh                 six real bundles and prefixes of lodash
#   ./scale.sh replicate       K renamed copies of lodash, K = 1 … 64 (timing only; the text of the modules is meaningless)
cd "$(dirname "$0")"
T=${T:-$(mktemp -d)}
LS=../../lean/.lake/build/bin/lowersplit

row() { # label bundle statements modules [replicate K]
  node ../splits/gen_spec.mjs "$3" blocks "$4" >"$T/spec.json"
  timeout 240 "$LS" "bundles/$2.json" "$T/spec.json" "$T/out" ${5:+--replicate "$5"} 2>&1 | awk -v label="$1" -v n="$3" -v k="$4" '
    /\[time\] read/ { json = $(NF - 1) }
    /\[time\] footprints/ { fp = $(NF - 1) }
    /\[time\] mkPlan/ { plan = $(NF - 1) }
    /\[time\] total/ { total = $(NF - 1) }
    /pass|FAIL/ && /ms\)/ { gsub(/[()]/, ""); t[$2] = $3; if ($1 == "FAIL") bad = bad " " $2 }
    /^ +FAIL/ && !/ms\)/ { bad = bad " front-end rule" }
    /\[time\] all checks/ { checks = $(NF - 1) }
    END { printf "| %s | %d | %d | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |\n", label, n, k, json, fp, plan, t["UniqueDefs"], t["ReadsClosed"], t["Faithful"], t["Linkable"], t["Hygienic"], t["Permutes"], t["OrderOk"], checks, (bad == "" ? "accepted" : "rejected:" bad) }'
}

echo "| Input | Statements | Modules | JSON | Footprints | mkPlan | UniqueDefs | ReadsClosed | Faithful | Linkable | Hygienic | Permutes | OrderOk | All checks | Verdict |"
echo "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
if [ "${1:-}" = replicate ]; then
  for K in 1 2 4 8 16 32 64; do row "lodash x$K" lodash $((670 * K)) $((6 * K)) $K; done
else
  for n in 100 200 300; do
    node prefix.mjs bundles/lodash.json $n >"$T/lodash_prefix.json"
    cp "$T/lodash_prefix.json" bundles/lodash_prefix.json
    row "lodash, first $n" lodash_prefix "$n" $((n / 25))
    rm -f bundles/lodash_prefix.json
  done
  row "semver" semver 59 4
  row "lodash" lodash 670 6
  row "acorn" acorn 447 5
  row "acorn, minified" acorn.min 447 5
  row "zod, minified" zod.min 1072 8
  row "zod" zod 1072 8
fi
