# Debundle scale regressions

This package holds synthetic tests tagged `manual` and `scale`. They remain
outside routine CI. Query every scale test with:

```sh
bbr query 'attr("tags", "scale", kind(".*_test rule", //devinfra/js/debundle/bench/...))'
```

Run the query and test on the same BuildBuddy runner so the labels stay clean
and every queried test is passed explicitly. Bazel omits `manual` tests when
expanding wildcard patterns, even without `--config=ci`; explicit labels
returned by the query still select them. The query must return at least one
test or the script fails. Standard Bazel test flags control repetition and
result reuse:

```sh
bbr --script="$(cat <<'SCRIPT'
set -euo pipefail
labels_file="$(mktemp)"
trap 'rm -f "$labels_file"' EXIT
bazelisk query 'attr("tags", "scale", kind(".*_test rule", //devinfra/js/debundle/bench/...))' > "$labels_file"
mapfile -t targets < "$labels_file"
if ((${#targets[@]} == 0)); then
  echo 'scale-test query returned no targets' >&2
  exit 1
fi
printf 'Selected %d scale tests:\n' "${#targets[@]}"
printf '  %s\n' "${targets[@]}"
bazelisk test -c opt --nocache_test_results --runs_per_test=3 "${targets[@]}"
SCRIPT
)"
```
