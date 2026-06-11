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
#     command.sh                  — rerunner stub
#     perf.data                   — raw perf samples
#     perf_report_children.txt    — tree report (sorted comm,dso,symbol)
#     perf_report_no_children.txt — flat report
#     perf_script_stacks.txt      — perf script (per-sample stacks)
#     perf_header.txt             — perf.data header
#     perf_evlist.txt             — recorded event list
#     stdout.txt                  — debundler stdout
#     perf_record_stderr.txt      — debundler+perf stderr
#
# `perf report` shells out to whatever `addr2line` it finds on $PATH for
# inlined-frame resolution. On Rust binaries with deep DWARF, the GNU
# addr2line is single-threaded and has no inter-invocation cache, so
# symbolization can dominate wall time (30+ minutes observed). LLVM's
# llvm-addr2line is command-line compatible with the flags perf uses
# (-e/-a/-i/-f) and is typically 10-50x faster on Rust DWARF thanks to a
# persistent in-process symbol cache.
#
# Two interlocking hacks make this work on Nix:
#
# 1. A shim dir that aliases `addr2line` -> `llvm-addr2line` and is
#    prepended to PATH.
#
# 2. The `perf` binary on Nix is a wrapper shell script that itself
#    re-prepends GNU binutils to PATH before exec'ing the real perf,
#    which shadows our shim. Bypass the wrapper by invoking the inner
#    `.perf-wrapped` binary directly when present.

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

"${perf_cmd}" record \
  -F 99 \
  -e cycles:u \
  --call-graph dwarf,8192 \
  -o "${profile_dir}/perf.data" \
  -- "$@" \
  >"${profile_dir}/stdout.txt" \
  2>"${profile_dir}/perf_record_stderr.txt"

"${perf_cmd}" report --stdio --input "${profile_dir}/perf.data" \
  --children --sort comm,dso,symbol \
  >"${profile_dir}/perf_report_children.txt"

"${perf_cmd}" report --stdio --input "${profile_dir}/perf.data" \
  --no-children --sort comm,dso,symbol \
  >"${profile_dir}/perf_report_no_children.txt"

"${perf_cmd}" script --input "${profile_dir}/perf.data" \
  >"${profile_dir}/perf_script_stacks.txt"

"${perf_cmd}" report --stdio --header-only --input "${profile_dir}/perf.data" \
  >"${profile_dir}/perf_header.txt"

"${perf_cmd}" evlist --input "${profile_dir}/perf.data" \
  >"${profile_dir}/perf_evlist.txt"
