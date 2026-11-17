#!/usr/bin/env bash
set -Eeuo pipefail

workspace="$(git rev-parse --show-toplevel)"
artifact_root="${BUILDBUDDY_ARTIFACTS_DIRECTORY:?BuildBuddy artifact directory is required}/debundle-performance"
mkdir -p "$artifact_root"
stage=validate
baseline_root=""
baseline_tmp=""

cleanup() {
  if [[ -n "$baseline_root" ]]; then
    git worktree remove --force "$baseline_root" || true
  fi
  if [[ -n "$baseline_tmp" ]]; then
    rm -rf "$baseline_tmp"
  fi
}

record_failure() {
  local status="$1"
  if ((status != 0)) && [[ ! -f "$artifact_root/failure.json" ]]; then
    python3 - "$artifact_root/failure.json" "$stage" "$status" <<'PY'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(
    json.dumps(
        {"complete": False, "stage": sys.argv[2], "exit_status": int(sys.argv[3])},
        indent=2,
        sort_keys=True,
    )
    + "\n"
)
PY
  fi
}

on_exit() {
  local status=$?
  cleanup
  if ((status != 0)); then
    record_failure "$status"
  fi
}
trap on_exit EXIT

candidate_sha="${DEBUNDLE_BENCH_CANDIDATE_SHA:?candidate SHA required}"
baseline_sha="${DEBUNDLE_BENCH_BASELINE_SHA:-}"
suite="${DEBUNDLE_BENCH_SUITE:-canonical}"
repetitions="${DEBUNDLE_BENCH_REPETITIONS:-3}"
profile_mode="${DEBUNDLE_BENCH_PROFILE_MODE:-none}"
IFS=, read -r -a invocation_ids <<<"${DEBUNDLE_BENCH_INVOCATION_IDS:-}"
invocation_index=0
invocation_log="$artifact_root/invocations.txt"
: >"$invocation_log"

next_invocation_id() {
  local role="$1"
  if ((invocation_index >= ${#invocation_ids[@]})); then
    echo "missing caller-supplied BuildBuddy invocation ID for $role" >&2
    return 2
  fi
  INVOCATION_ID="${invocation_ids[invocation_index]}"
  ((invocation_index += 1))
  printf '%s %s\n' "$role" "$INVOCATION_ID" >>"$invocation_log"
}

[[ "$(git rev-parse HEAD)" == "$candidate_sha" ]]
[[ "$candidate_sha" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]]
if [[ -n "$baseline_sha" ]]; then
  [[ "$baseline_sha" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]]
fi
[[ "$suite" == canonical || "$suite" == scales || "$suite" == all ]]
[[ "$profile_mode" == none || "$profile_mode" == cpu || "$profile_mode" == heap ]]
[[ "$repetitions" =~ ^([1-9]|10)$ ]]
if [[ "$suite" == scales && "$profile_mode" != none ]]; then
  echo "CPU/heap profiles cover the native canonical Analyze phase; choose canonical or all." >&2
  exit 2
fi

if [[ -n "$baseline_sha" ]]; then
  stage=fetch-baseline
  git fetch --no-tags origin "$baseline_sha"
  baseline_tmp="$(mktemp -d "${TMPDIR:-/tmp}/debundle-baseline.XXXXXXXX")"
  git worktree add --detach "$baseline_tmp/worktree" "$baseline_sha"
  baseline_root="$baseline_tmp/worktree"
fi

rbe_image="${RBE_CONTAINER_IMAGE:?RBE container image missing from bb-remote action}"
bazel_common=(--config=rbe --config=ci "--remote_default_exec_properties=container-image=docker://$rbe_image")
tools=(
  //devinfra/js/debundle:debundle_project_analysis
  //devinfra/js/debundle:debundle_analyze
  //devinfra/js/debundle:debundle
)

build_engine() {
  local name="$1"
  local root="$2"
  stage="build-$name-tools"
  local invocation_id
  next_invocation_id "$name-build"
  invocation_id="$INVOCATION_ID"
  (
    cd "$root"
    bazelisk build "${bazel_common[@]}" --invocation_id="$invocation_id" \
      --remote_download_outputs=all -c opt "${tools[@]}"
  ) >"$artifact_root/$name-build.log" 2>&1
}

if [[ "$suite" != scales ]]; then
  stage=build-candidate
  next_invocation_id candidate-build
  candidate_build_id="$INVOCATION_ID"
  bazelisk build "${bazel_common[@]}" --invocation_id="$candidate_build_id" \
    --remote_download_outputs=all -c opt \
    //devinfra/js/debundle/perf:benchmark_bin \
    //devinfra/js/debundle/excalidraw:canonical_summary \
    "${tools[@]}" >"$artifact_root/candidate-build.log" 2>&1
  stage=candidate-canonical-checks
  next_invocation_id candidate-checks
  candidate_checks_id="$INVOCATION_ID"
  bazelisk test "${bazel_common[@]}" --invocation_id="$candidate_checks_id" -c opt --nocache_test_results \
    //devinfra/js/debundle/excalidraw:test_debundle_tree \
    //devinfra/js/debundle/excalidraw:spec_output_test \
    //devinfra/js/debundle/excalidraw:test_smoke \
    >"$artifact_root/candidate-canonical-tests.log" 2>&1
  expected_summary="$(bazelisk "${bazel_common[@]}" -c opt info bazel-bin)/devinfra/js/debundle/excalidraw/canonical_summary.json"
  candidate_bin="$(bazelisk "${bazel_common[@]}" -c opt info bazel-bin)/devinfra/js/debundle"
  if [[ -n "$baseline_sha" ]]; then
    build_engine baseline "$baseline_root"
    baseline_bin="$(cd "$baseline_root" && bazelisk "${bazel_common[@]}" -c opt info bazel-bin)/devinfra/js/debundle"
    mkdir -p "$artifact_root/canonical/preparation"
    stage=native-canonical-pipeline
    next_invocation_id native-canonical-pipeline
    benchmark_id="$INVOCATION_ID"
    bazelisk run "${bazel_common[@]}" --invocation_id="$benchmark_id" \
      --remote_download_outputs=all -c opt //devinfra/js/debundle/perf:benchmark_bin -- \
      --workspace "$workspace" \
      --candidate-binary-dir "$candidate_bin" \
      --candidate-sha "$candidate_sha" \
      --baseline-binary-dir "$baseline_bin" \
      --baseline-sha "$baseline_sha" \
      --repetitions "$repetitions" \
      --profile-mode "$profile_mode" \
      --output-dir "$artifact_root/canonical" \
      --expected-summary "$expected_summary"
  else
    mkdir -p "$artifact_root/canonical/preparation"
    stage=native-canonical-pipeline
    next_invocation_id native-canonical-pipeline
    benchmark_id="$INVOCATION_ID"
    bazelisk run "${bazel_common[@]}" --invocation_id="$benchmark_id" \
      --remote_download_outputs=all -c opt //devinfra/js/debundle/perf:benchmark_bin -- \
      --workspace "$workspace" \
      --candidate-binary-dir "$candidate_bin" \
      --candidate-sha "$candidate_sha" \
      --repetitions "$repetitions" \
      --profile-mode "$profile_mode" \
      --output-dir "$artifact_root/canonical" \
      --expected-summary "$expected_summary"
  fi
fi

if [[ "$suite" == scales || "$suite" == all ]]; then
  mapfile -t scale_targets < <(sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' \
    "$workspace/devinfra/js/debundle/perf/scale_targets.txt")
  test_scale_engine() {
    local name="$1"
    local root="$2"
    local revision="$3"
    local invocation_id
    next_invocation_id "$name-scales"
    invocation_id="$INVOCATION_ID"
    stage="$name-scale-suite"
    if (
      cd "$root"
      bazelisk test "${bazel_common[@]}" --invocation_id="$invocation_id" -c opt \
        --nocache_test_results \
        --test_tag_filters=manual \
        --runs_per_test="$repetitions" \
        --test_timeout=1800 \
        --jobs=1 \
        --local_test_jobs=1 \
        --run_under='prlimit --as=17179869184 --fsize=536870912 --' \
        "${scale_targets[@]}"
    ) >"$artifact_root/$name-scale-tests.log" 2>&1; then
      status=0
    else
      status=$?
    fi
    python3 - "$artifact_root/$name-scale-metrics.json" "$name" "$revision" "$status" "$repetitions" "${scale_targets[@]}" <<'PY'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "engine": sys.argv[2],
            "revision": sys.argv[3],
            "exit_status": int(sys.argv[4]),
            "complete": int(sys.argv[4]) == 0,
            "repetitions_requested": int(sys.argv[5]),
            "targets": sys.argv[6:],
            "resource_limit": {
                "kind": "address_space_per_process",
                "address_space_bytes": 17179869184,
                "file_size_bytes_per_file": 536870912,
                "timeout_seconds_per_test": 1800,
            },
            "note": "Bazel test logs and per-test outputs are the source evidence; no aggregate runtime/RSS bound is asserted.",
        },
        indent=2,
        sort_keys=True,
    )
    + "\n"
)
PY
    if ((status != 0)); then
      return "$status"
    fi
  }

  test_scale_engine candidate "$workspace" "$candidate_sha"
  if [[ -n "$baseline_sha" ]]; then
    test_scale_engine baseline "$baseline_root" "$baseline_sha"
  fi
fi

if ((invocation_index != ${#invocation_ids[@]})); then
  echo "unused caller-supplied BuildBuddy invocation IDs: $((${#invocation_ids[@]} - invocation_index))" >&2
  exit 2
fi

stage=complete
trap - EXIT
cleanup
