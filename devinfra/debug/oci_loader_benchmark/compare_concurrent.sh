#!/usr/bin/env bash
set -euo pipefail

driver="$TEST_SRCDIR/$TEST_WORKSPACE/$1"
prime="$TEST_SRCDIR/$TEST_WORKSPACE/$2"
bare="$TEST_SRCDIR/$TEST_WORKSPACE/$3"
guarded="$TEST_SRCDIR/$TEST_WORKSPACE/$4"
python_tag='ducktape-oci-benchmark:python-herd'
TIMEFORMAT='elapsed=%3R user=%3U system=%3S'

for tag in ducktape-oci-benchmark:rules-0 ducktape-oci-benchmark:rules-1 ducktape-oci-benchmark:rules-2 "$python_tag"; do
  if docker image inspect "$tag" >/dev/null 2>&1; then
    echo "unexpected preexisting benchmark tag $tag" >&2
    exit 1
  fi
done

echo 'CASE prime layer store'
time "$prime"

run_four() {
  local prefix="$1"
  shift
  local pid
  local failed=0
  local pids=()
  for index in 0 1 2 3; do
    "$@" >"${TEST_UNDECLARED_OUTPUTS_DIR}/${prefix}-caller-$index.log" 2>&1 &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
      failed=1
    fi
  done
  return "$failed"
}

echo 'CASE four python guarded callers, fresh tag, warm layers'
time run_four python "$driver" python "$python_tag"
echo 'CASE four bare rules_oci callers, fresh tag, warm layers'
time run_four bare "$bare"
echo 'CASE four guarded rules_oci callers, fresh tag, warm layers'
time run_four guarded "$driver" guarded_rules ducktape-oci-benchmark:rules-2 "$guarded"
for tag in "$python_tag" ducktape-oci-benchmark:rules-1 ducktape-oci-benchmark:rules-2; do
  docker image inspect --format '{{.Id}}' "$tag"
done
