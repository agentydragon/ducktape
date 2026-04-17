#!/usr/bin/env bash
# Writes $SESSION_DIR/buildbuddy.bazelrc from $BUILDBUDDY_API_KEY.
set -euo pipefail

: "${SESSION_DIR:?required}"

if [[ -z ${BUILDBUDDY_API_KEY:-} ]]; then
  echo "skip: BUILDBUDDY_API_KEY not set"
  exit 0
fi

rc="$SESSION_DIR/buildbuddy.bazelrc"
cat >"$rc" <<EOF
common --remote_header=x-buildbuddy-api-key=$BUILDBUDDY_API_KEY
build --config=rbe
EOF
echo "wrote $rc"
echo "export BUILDBUDDY_BAZELRC=$rc" >>"$ENV_OUT"
