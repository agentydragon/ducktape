#!/usr/bin/env bash
#
# perf_wrapper.sh — run the debundler under `perf record` and emit a tree of
# perf reports next to a rerunnable command stub.
#
# Standalone tool. Invoke directly with the debundler binary + args; the
# wrapper handles the `perf record` invocation, the addr2line shim, and
# the nix-perf-wrapper bypass. Suitable for ad-hoc profiling on a
# rebuilt opt binary against a pre-staged input set.
#
# Usage:
#     perf_wrapper.sh --output-dir <dir> -- <debundler> [args...]
#
# Outputs (under <dir>):
#     command.sh                    — rerunner stub
#     perf.data                     — raw perf samples
#     perf_report_flat_symbols.txt  — fast no-callgraph flat report
#     perf_report_children.txt      — tree report (sorted comm,dso,symbol)
#     perf_report_no_children.txt   — flat report with callgraph context
#     perf_script_stacks_head.txt   — bounded early perf script stack slice
#     perf_script_stacks_mid.txt    — bounded middle perf script stack slice
#     perf_script_stacks_late.txt   — bounded late perf script stack slice
#     perf_script_stacks.txt        — full perf script output, if it finishes
#     perf_header.txt               — perf.data header
#     perf_evlist.txt               — recorded event list
#     stdout.txt                    — debundler stdout
#     perf_record_stderr.txt        — debundler+perf stderr
#
# Knobs:
#     PERF_RECORD_FREQ          sample frequency; default 99
#     PERF_CALL_GRAPH           perf call graph mode; default dwarf,8192
#     PERF_ADDR2LINE_STYLE      report symbolizer style; default llvm
#                               use "default" to let perf choose
#     PERF_POSTPROCESS_TIMEOUT  per-report timeout; default 120s
#     PERF_CALLGRAPH_TIMEOUT    per-stack-slice timeout; default 20s
#     PERF_CALLGRAPH_MAX_STACK  stack depth for perf script slices; default 48
#     PERF_CALLGRAPH_SLICE_SECS seconds in each stack slice; default 6
#
# Report generation defaults to llvm-style symbolization and bounded
# post-processing. Timeout metadata lands in <output>.failed.txt; full-report
# partial output lands in <output>.partial.
#
# A remaining Nix compatibility shim aliases addr2line -> llvm-addr2line when
# that binary is available. This mostly helps commands that do not support
# perf's `--addr2line-style` flag.
#
# The `perf` binary on Nix is itself a wrapper shell script that re-prepends GNU
# binutils to PATH before exec'ing the real perf, which can shadow that shim.
# Bypass the wrapper by invoking the inner `.perf-wrapped` binary directly when
# present.

set -euo pipefail

usage() {
  echo "usage: perf_wrapper.sh --output-dir <dir> -- <debundler> [args...]" >&2
  exit 2
}

profile_dir=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir)
      profile_dir="$2"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    *)
      usage
      ;;
  esac
done

if [[ -z "${profile_dir}" || $# -eq 0 ]]; then
  usage
fi

mkdir -p "${profile_dir}"

# Rerunner stub: the action shell already expanded any ${OLDPWD}/...
# references in the debundler argv to absolute paths, so the recorded
# command is portable across invocations. %q quoting preserves spaces and
# special characters.
{
  echo "#!/usr/bin/env bash"
  echo "set -euo pipefail"
  printf 'exec'
  for arg in "$@"; do
    printf ' %q' "${arg}"
  done
  printf '\n'
} >"${profile_dir}/command.sh"
chmod +x "${profile_dir}/command.sh"

command -v perf >/dev/null || {
  echo 'perf not found on PATH' >&2
  exit 127
}

# Shim 1: prepend a dir that aliases addr2line -> llvm-addr2line.
if command -v llvm-addr2line >/dev/null; then
  shim_dir="${profile_dir}/.symbolizer-shim"
  mkdir -p "${shim_dir}"
  ln -sf "$(command -v llvm-addr2line)" "${shim_dir}/addr2line"
  export PATH="${shim_dir}:${PATH}"
fi

# Shim 2: bypass nix's perf wrapper (which re-prepends GNU binutils) by
# invoking the inner .perf-wrapped binary directly when present.
perf_real="$(readlink -f "$(command -v perf)")"
perf_wrapped="$(dirname "${perf_real}")/.perf-wrapped"
if [[ -x "${perf_wrapped}" ]]; then
  perf_cmd="${perf_wrapped}"
else
  perf_cmd="$(command -v perf)"
fi

postprocess_timeout="${PERF_POSTPROCESS_TIMEOUT:-120s}"
record_frequency="${PERF_RECORD_FREQ:-99}"
call_graph="${PERF_CALL_GRAPH:-dwarf,8192}"
addr2line_style="${PERF_ADDR2LINE_STYLE:-llvm}"
callgraph_timeout="${PERF_CALLGRAPH_TIMEOUT:-20s}"
callgraph_max_stack="${PERF_CALLGRAPH_MAX_STACK:-48}"
callgraph_slice_secs="${PERF_CALLGRAPH_SLICE_SECS:-6}"
perf_report_symbolizer_args=()
if [[ -n "${addr2line_style}" && "${addr2line_style}" != "default" ]]; then
  perf_report_symbolizer_args+=(--addr2line-style "${addr2line_style}")
fi

{
  echo "PERF_RECORD_FREQ=${record_frequency}"
  echo "PERF_CALL_GRAPH=${call_graph}"
  echo "PERF_ADDR2LINE_STYLE=${addr2line_style}"
  echo "PERF_POSTPROCESS_TIMEOUT=${postprocess_timeout}"
  echo "PERF_CALLGRAPH_TIMEOUT=${callgraph_timeout}"
  echo "PERF_CALLGRAPH_MAX_STACK=${callgraph_max_stack}"
  echo "PERF_CALLGRAPH_SLICE_SECS=${callgraph_slice_secs}"
  echo "PERF_CMD=${perf_cmd}"
  if command -v addr2line >/dev/null; then
    echo "ADDR2LINE=$(command -v addr2line)"
  fi
} >"${profile_dir}/perf_wrapper_env.txt"

run_perf_command() {
  local output="$1"
  local timeout_value="$2"
  local failure_message="$3"
  local partial_suffix="$4"
  shift 4
  local tmp_output="${output}.tmp"
  local tmp_stderr="${output}.stderr.tmp"

  rm -f "${tmp_output}" "${tmp_stderr}" "${output}.failed.txt" "${output}.partial"
  if timeout --foreground "${timeout_value}" \
    "${perf_cmd}" "$@" >"${tmp_output}" 2>"${tmp_stderr}"; then
    mv "${tmp_output}" "${output}"
    if [[ -s "${tmp_stderr}" ]]; then
      mv "${tmp_stderr}" "${output}.stderr"
    else
      rm -f "${tmp_stderr}" "${output}.stderr"
    fi
    return 0
  fi

  local status=$?
  {
    echo "${failure_message}"
    echo "status=${status}"
    echo "timeout=${timeout_value}"
    printf 'command=%q' "${perf_cmd}"
    for arg in "$@"; do
      printf ' %q' "${arg}"
    done
    printf '\n'
    if [[ -s "${tmp_stderr}" ]]; then
      echo
      echo "stderr:"
      cat "${tmp_stderr}"
    fi
  } >"${output}.failed.txt"
  if [[ -s "${tmp_output}" ]]; then
    mv "${tmp_output}" "${output}${partial_suffix}"
  else
    rm -f "${tmp_output}"
  fi
  rm -f "${tmp_stderr}"
  return 0
}

run_perf_output() {
  local output="$1"
  shift
  run_perf_command \
    "${output}" \
    "${postprocess_timeout}" \
    "perf post-processing command failed" \
    .partial \
    "$@"
}

run_perf_stack_slice() {
  local output="$1"
  shift
  run_perf_command \
    "${output}" \
    "${callgraph_timeout}" \
    "perf stack-slice command failed" \
    "" \
    "$@"
}

extract_stack_slices() {
  local header="${profile_dir}/perf_header.txt"
  local first_sample=""
  local last_sample=""

  if [[ -s "${header}" ]]; then
    first_sample="$(awk -F': ' '/# time of first sample/ { print $2 }' "${header}")"
    last_sample="$(awk -F': ' '/# time of last sample/ { print $2 }' "${header}")"
  fi
  if [[ -z "${first_sample}" || -z "${last_sample}" ]]; then
    run_perf_stack_slice "${profile_dir}/perf_script_stacks_head.txt" \
      script --input "${profile_dir}/perf.data" \
      --max-stack "${callgraph_max_stack}"
    return 0
  fi

  read -r head_start head_end mid_start mid_end late_start late_end < <(
    awk -v first="${first_sample}" \
      -v last="${last_sample}" \
      -v requested_window="${callgraph_slice_secs}" \
      'BEGIN {
        duration = last - first
        window = requested_window
        if (duration <= 0) {
          printf "%.6f %.6f %.6f %.6f %.6f %.6f\n", first, last, first, last, first, last
          exit
        }
        if (window <= 0 || window > duration) {
          window = duration
        }
        mid = first + duration / 2
        printf "%.6f %.6f %.6f %.6f %.6f %.6f\n",
          first, first + window,
          mid - window / 2, mid + window / 2,
          last - window, last
      }'
  )

  run_perf_stack_slice "${profile_dir}/perf_script_stacks_head.txt" \
    script --input "${profile_dir}/perf.data" \
    --max-stack "${callgraph_max_stack}" \
    --time "${head_start},${head_end}"

  run_perf_stack_slice "${profile_dir}/perf_script_stacks_mid.txt" \
    script --input "${profile_dir}/perf.data" \
    --max-stack "${callgraph_max_stack}" \
    --time "${mid_start},${mid_end}"

  run_perf_stack_slice "${profile_dir}/perf_script_stacks_late.txt" \
    script --input "${profile_dir}/perf.data" \
    --max-stack "${callgraph_max_stack}" \
    --time "${late_start},${late_end}"
}

"${perf_cmd}" record \
  -F "${record_frequency}" \
  -e cycles:u \
  --call-graph "${call_graph}" \
  -o "${profile_dir}/perf.data" \
  -- "$@" \
  >"${profile_dir}/stdout.txt" \
  2>"${profile_dir}/perf_record_stderr.txt"

run_perf_output "${profile_dir}/perf_header.txt" \
  report "${perf_report_symbolizer_args[@]}" \
  --stdio --header-only --input "${profile_dir}/perf.data"

run_perf_output "${profile_dir}/perf_evlist.txt" \
  evlist --input "${profile_dir}/perf.data"

run_perf_output "${profile_dir}/perf_report_flat_symbols.txt" \
  report "${perf_report_symbolizer_args[@]}" \
  --stdio --input "${profile_dir}/perf.data" \
  --no-children -g none --percent-limit 0.5 --sort symbol

extract_stack_slices

run_perf_output "${profile_dir}/perf_report_children.txt" \
  report "${perf_report_symbolizer_args[@]}" \
  --stdio --input "${profile_dir}/perf.data" \
  --children --sort comm,dso,symbol

run_perf_output "${profile_dir}/perf_report_no_children.txt" \
  report "${perf_report_symbolizer_args[@]}" \
  --stdio --input "${profile_dir}/perf.data" \
  --no-children --sort comm,dso,symbol

run_perf_output "${profile_dir}/perf_script_stacks.txt" \
  script --input "${profile_dir}/perf.data"
