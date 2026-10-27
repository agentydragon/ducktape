#!/usr/bin/env bash
set -euo pipefail

driver="$TEST_SRCDIR/$TEST_WORKSPACE/$1"
shift
scripts=("$@")
first_importer="${FIRST_IMPORTER:-rules}"
if [[ "$first_importer" != rules && "$first_importer" != python ]]; then
  echo "FIRST_IMPORTER must be rules or python" >&2
  exit 1
fi
TIMEFORMAT='elapsed=%3R user=%3U system=%3S'
docker info --format 'DRIVER={{.Driver}} ROOT={{.DockerRootDir}} SERVER={{.ServerVersion}}'
printf 'TMPDIR=%s TEST_TMPDIR=%s DOCKER_HOST=%s\n' "${TMPDIR:-}" "${TEST_TMPDIR:-}" "${DOCKER_HOST:-}"
echo 'CASE python startup control'
time "$driver" noop
for index in 0 1 2; do
  script="$TEST_SRCDIR/$TEST_WORKSPACE/${scripts[$index]}"
  python_tag="ducktape-oci-benchmark:python-$index"
  rules_tag="ducktape-oci-benchmark:rules-$index"
  for tag in "$python_tag" "$rules_tag"; do
    if docker image inspect "$tag" >/dev/null 2>&1; then
      echo "unexpected preexisting benchmark tag $tag" >&2
      exit 1
    fi
  done
  if [[ "$first_importer" == python && $((index % 2)) == 0 ]] \
    || [[ "$first_importer" == rules && $((index % 2)) == 1 ]]; then
    order=(python rules)
  else
    order=(rules python)
  fi
  for importer in "${order[@]}"; do
    echo "CASE $index $importer cold-tag"
    if [[ "$importer" == python ]]; then
      time "$driver" python "$python_tag"
    else
      time "$script"
    fi
  done
  echo "CASE $index python warm"
  time "$driver" python "$python_tag"
  echo "CASE $index rules warm"
  time "$script"
  echo "CASE $index guarded_rules warm-tag-missing-marker"
  time "$driver" guarded_rules "$rules_tag" "$script"
  echo "CASE $index guarded_rules second-warm"
  time "$driver" guarded_rules "$rules_tag" "$script"
done
