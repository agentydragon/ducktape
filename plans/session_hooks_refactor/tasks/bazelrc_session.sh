#!/usr/bin/env bash
# Composes $SESSION_DIR/bazelrc from the bazelrc fragments produced by upstream tasks.
set -euo pipefail

: "${SESSION_DIR:?required}"

rc="$SESSION_DIR/bazelrc"
: >"$rc"
echo "startup --host_jvm_args=-Xmx4g" >>"$rc"
[[ -n ${BUILDBUDDY_BAZELRC:-} ]] && echo "try-import $BUILDBUDDY_BAZELRC" >>"$rc"
[[ -n ${BBR_BAZELRC:-} ]] && echo "try-import $BBR_BAZELRC" >>"$rc"
[[ -n ${BES_SOCK:-} ]] && echo "build --bes_backend=grpc://unix:$BES_SOCK" >>"$rc"

echo "wrote $rc ($(wc -l <"$rc") lines)"
echo "export SESSION_BAZELRC=$rc" >>"$ENV_OUT"
