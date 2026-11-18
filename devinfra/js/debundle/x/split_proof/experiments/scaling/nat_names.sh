#!/usr/bin/env bash
# How much of the kernel time is comparing names? The same synthetic bundles, checked with `decide +kernel`
# against the real sources (`Name := String`) and against a scratch copy of the core Lean files with `Name := Nat`.
#
#   ./nat_names.sh [statements …]        default: 48 96 144 192
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
lean="$here/../../lean"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/EsmSplit"
for f in Bundle Plan Semantics Correct Fast Split; do
  sed -e 's/^abbrev Name := String/abbrev Name := Nat/' -e 's/^abbrev ModName := String/abbrev ModName := Nat/' \
    "$lean/EsmSplit/$f.lean" >"$work/EsmSplit/$f.lean"
done
cp "$lean/lean-toolchain" "$work/"
cat >"$work/lakefile.toml" <<'LAKE'
name = "nat_names"
defaultTargets = ["EsmSplit"]

[[lean_lib]]
name = "EsmSplit"
globs = ["EsmSplit.+"]
LAKE
(cd "$work" && lake build >/dev/null)

measure() { # dir file: "<seconds> s, peak <MB> MB, <exit …>"
  (cd "$1" && python3 "$here/peak.py" --timeout 900 -- lake env lean "$2")
}

echo "statements | names as strings | names as numbers"
for n in "${@:-48 96 144 192}"; do
  python3 "$here/gen.py" "$n" $((n / 6)) kernel >"$work/strings.lean"
  python3 "$here/gen.py" "$n" $((n / 6)) kernel nat >"$work/nat.lean"
  echo "$n | $(measure "$lean" "$work/strings.lean") | $(measure "$work" "$work/nat.lean")"
done
