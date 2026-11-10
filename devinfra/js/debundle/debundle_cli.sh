#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: bazel run <target> -- <debundle-command> [args...]

Example:
  bazel run //path/to:debundle_cli -- graph-summary --format json
EOF
}

fail() {
  printf '%s\n' "$1" >&2
  exit 2
}

debundler_runfile=""
graph_runfile=""
graph_relpath=""
modules_path=""
source_mode=""
source_value=""
spec_root=""
graph_source_runfile=""
package_root_args=()

while (($#)); do
  case "$1" in
    --debundler)
      (($# >= 2)) || fail "missing value for --debundler"
      debundler_runfile="$2"
      shift 2
      ;;
    --graph)
      (($# >= 2)) || fail "missing value for --graph"
      graph_runfile="$2"
      shift 2
      ;;
    --graph-relpath)
      (($# >= 2)) || fail "missing value for --graph-relpath"
      graph_relpath="$2"
      shift 2
      ;;
    --modules-path)
      (($# >= 2)) || fail "missing value for --modules-path"
      modules_path="$2"
      shift 2
      ;;
    --source-root)
      (($# >= 3)) || fail "missing value for --source-root"
      source_mode="$2"
      source_value="$3"
      shift 3
      ;;
    --spec-root)
      (($# >= 2)) || fail "missing value for --spec-root"
      spec_root="$2"
      shift 2
      ;;
    --graph-source-root)
      (($# >= 2)) || fail "missing value for --graph-source-root"
      graph_source_runfile="$2"
      shift 2
      ;;
    --package-root)
      (($# >= 3)) || fail "missing value for --package-root"
      package_root_args+=(--graph-package-root "$2=$(rlocation "$3")")
      shift 3
      ;;
    --)
      shift
      break
      ;;
    *)
      fail "unexpected launcher argument: $1"
      ;;
  esac
done

if (($# > 0)) && [[ "$1" == "--" ]]; then
  shift
fi

if [[ -z "$debundler_runfile" || -z "$graph_runfile" || -z "$graph_relpath" || -z "$modules_path" || -z "$source_mode" ]]; then
  usage
  exit 2
fi
if (($# == 0)); then
  usage
  exit 2
fi

workspace="${BUILD_WORKSPACE_DIRECTORY:-$PWD}"
debundler="$(rlocation "$debundler_runfile")"
graph_root="$(rlocation "$graph_runfile")"

case "$source_mode" in
  workspace)
    source_root="$workspace/$source_value"
    ;;
  runfile)
    source_root="$(rlocation "$source_value")"
    ;;
  graph-source)
    [[ -n "$graph_source_runfile" ]] || fail "graph-source source_root requires graph_source_data"
    source_root="$(rlocation "$graph_source_runfile")/$source_value"
    ;;
  *)
    fail "unknown source-root mode: $source_mode"
    ;;
esac

graph_root_args=()
if [[ -n "$spec_root" ]]; then
  graph_root_args+=(--spec-root "$workspace/$spec_root")
fi
if [[ -n "$graph_source_runfile" ]]; then
  graph_root_args+=(--graph-source-root "$(rlocation "$graph_source_runfile")")
fi

export DEBUNDLE_MODULES="$workspace/$modules_path"
export DEBUNDLE_SOURCE_ROOT="$source_root"
export DEBUNDLE_GRAPH="$graph_root/$graph_relpath"

# Remote-runner downloads may omit execute mode on nested runfiles.
if [[ ! -x "$debundler" ]]; then
  chmod u+x "$debundler"
fi

cd "$workspace"
exec "$debundler" "${graph_root_args[@]}" "${package_root_args[@]}" "$@"
