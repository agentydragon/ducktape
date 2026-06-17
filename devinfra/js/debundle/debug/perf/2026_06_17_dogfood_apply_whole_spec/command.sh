# Rerunner: whole-spec synthesize-selectors under callgrind (Ir-only, no cache-sim).
# Run from a built `-c opt` debundle binary; gaffer spec + upstream snapshot as input.
BIN=bazel-bin/devinfra/js/debundle/debundle
GSPEC=../../../gaffer-private/tana/re/web/78d928dca7/spec
SROOT=../../../gaffer-private/tana/upstream/web/snapshots/78d928dca7
valgrind --tool=callgrind --callgrind-out-file=callgrind.whole.out \
  --cache-sim=no --branch-sim=no --dump-instr=no --collect-jumps=no \
  "$BIN" spec synthesize-selectors --modules "$GSPEC/modules" \
  --source-root "$SROOT" --chunk static/index-DI2GynTv.js --format json >/dev/null
