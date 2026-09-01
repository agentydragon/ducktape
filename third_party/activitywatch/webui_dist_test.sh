#!/usr/bin/env bash
set -euo pipefail

dist="$1"
main_js=("$dist"/assets/index-*.js)

[[ -f "$dist/index.html" ]]
[[ -f "$dist/sw.js" ]]
[[ ${#main_js[@]} -eq 1 ]]
grep -qF 'crossorigin="use-credentials"' "$dist/index.html"
grep -qF '.COMMIT_HASH="bazel"' "${main_js[0]}"
! grep -qF 'importScripts(' "$dist/sw.js"
