#!/usr/bin/env bash
set -euo pipefail
render=$(rlocation "$1")
node=$(rlocation "$2")
bundle=$(rlocation "$3")
"$node" "$bundle" >"$TEST_TMPDIR/bundle.out"
"$render" "$TEST_TMPDIR/split"
"$node" "$TEST_TMPDIR/split/main.mjs" >"$TEST_TMPDIR/split.out"
diff -u "$TEST_TMPDIR/bundle.out" "$TEST_TMPDIR/split.out"

"$render" "$TEST_TMPDIR/cycle" --variant cycle
"$node" "$TEST_TMPDIR/cycle/main.mjs" >"$TEST_TMPDIR/cycle.out"
diff -u "$TEST_TMPDIR/bundle.out" "$TEST_TMPDIR/cycle.out"

for control in effect_order cycle_tdz claimed_twice missing_import dropped_module; do
  status=0
  "$render" "$TEST_TMPDIR/$control" --force "$control" >"$TEST_TMPDIR/$control.check" || status=$?
  if [[ $status != 1 ]]; then
    echo "$control: expected the checker to reject with exit code 1, got $status" >&2
    exit 1
  fi
  status=0
  "$node" "$TEST_TMPDIR/$control/main.mjs" >"$TEST_TMPDIR/$control.out" 2>"$TEST_TMPDIR/$control.err" || status=$?
  case "$control" in
    cycle_tdz | claimed_twice | missing_import)
      if [[ $status == 0 ]]; then
        echo "$control: expected Node to reject the emitted modules" >&2
        exit 1
      fi
      ;;
    *)
      if [[ $status != 0 ]] || cmp -s "$TEST_TMPDIR/bundle.out" "$TEST_TMPDIR/$control.out"; then
        echo "$control: expected successful Node output that differs from the bundle" >&2
        exit 1
      fi
      ;;
  esac
done
