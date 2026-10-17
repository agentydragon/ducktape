#!/usr/bin/env bash
# Bazel inside a claude-ai build box (an agentplane sandbox): `box_bazel.sh <command> [args...]`.
# Deviation from the RBE-only rule (<../../devinfra/docs/rbe_workflows.md>): the box's egress
# refuses BuildBuddy, so Bazel runs locally on the workspace bazelrc minus its RBE import. The box
# brings the rest: `bazel`, and a system rc that trusts the egress proxy's CA and passes the proxy
# variables and KUBECONFIG to tests.
set -euo pipefail

src=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
rbe_import='import %workspace%/devinfra/bazel/rbe.bazelrc'

grep -qx "$rbe_import" "$src/.bazelrc" || {
  echo "box_bazel.sh: $src/.bazelrc no longer has the line: $rbe_import" >&2
  exit 1
}
{
  grep -vx "$rbe_import" "$src/.bazelrc"
  # Lint is CI's job; a box needs the targets themselves.
  echo 'build --config=nolint'
  echo 'common --config=ai_agent'
} >"$HOME/box.bazelrc"

cd "$src"
exec bazel --noworkspace_rc --bazelrc="$HOME/box.bazelrc" "$@"
