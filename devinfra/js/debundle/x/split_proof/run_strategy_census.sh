#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

day=$(date +%Y_%m_%d)
scratch="${TMPDIR:-/tmp}/agents/$day/split-proof-strategy-census"
mkdir -p "$scratch"

if (($#)); then
  targets=("$@")
else
  targets=(
    //devinfra/js/debundle/x/split_proof/translation/linked/...
    //devinfra/js/debundle/x/split_proof/translation/linked:module_graph_scale_test
    //devinfra/js/debundle/x/split_proof/debundle_cases:strategy_census_cases_test
    //devinfra/js/debundle/x/split_proof/debundle_cases:strategy_census_pinned_test
    //devinfra/js/debundle/x/split_proof/integration/library_splits:library_certificate_test
    //devinfra/js/debundle/x/split_proof/integration/excalidraw:excalidraw_scroll_certificate
    //devinfra/js/debundle/x/split_proof/integration/excalidraw:excalidraw_scroll_single_certificate
    //devinfra/js/debundle/x/split_proof/integration/excalidraw:native_metadata_browser_test
    //devinfra/js/debundle/x/split_proof/integration/excalidraw:bare_import_browser_test
  )
fi

invocation_file="$scratch/invocation-id"
log_file="$scratch/test.log"
set +e
bbr --invocation-id-file="$invocation_file" test "${targets[@]}" \
  --test_env=SPLIT_PROOF_STRATEGY_CENSUS=1 \
  --test_output=all \
  --remote_build_event_upload=all 2>&1 | tee "$log_file"
test_status=${PIPESTATUS[0]}
set -e

if [[ -s $invocation_file ]]; then
  artifacts="$scratch/artifacts"
  rm -rf "$artifacts"
  mkdir -p "$artifacts"
  if [[ -n ${SPLIT_PROOF_CENSUS_LOCAL_BUILDBUDDY_API_KEY:-} ]]; then
    BUILDBUDDY_API_KEY="$SPLIT_PROOF_CENSUS_LOCAL_BUILDBUDDY_API_KEY" \
      python3 devinfra/js/debundle/x/split_proof/download_strategy_census.py "$(cat "$invocation_file")" "$artifacts"
  else
    python3 devinfra/js/debundle/x/split_proof/download_strategy_census.py "$(cat "$invocation_file")" "$artifacts"
  fi
  python3 devinfra/js/debundle/x/split_proof/strategy_census_report.py "$artifacts" >"$scratch/strategy-census.md"
  cat "$scratch/strategy-census.md"
else
  echo "No BuildBuddy invocation ID was recorded; see $log_file" >&2
  exit 1
fi

echo "Census report: $scratch/strategy-census.md"
exit "$test_status"
