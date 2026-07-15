#!/usr/bin/env bash
set -euo pipefail

RUN_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CHECKOUT=${COLIBRI_CHECKOUT:-/var/lib/colibri/src/colibri}
REVISION=6d3ed7e62b1b4c05d8e656a5263e91b983aa26ba

if [[ ! -d "$CHECKOUT/.git" ]]; then
  mkdir -p "$(dirname "$CHECKOUT")"
  git clone https://github.com/JustVugg/colibri.git "$CHECKOUT"
fi

git -C "$CHECKOUT" fetch origin "$REVISION"
git -C "$CHECKOUT" checkout --detach "$REVISION"
cp "$RUN_DIR/flake.nix" "$RUN_DIR/flake.lock" "$CHECKOUT/"

if git -C "$CHECKOUT" apply --check "$RUN_DIR/benchmark_cuda_fixture.patch"; then
  git -C "$CHECKOUT" apply "$RUN_DIR/benchmark_cuda_fixture.patch"
elif ! git -C "$CHECKOUT" apply --reverse --check "$RUN_DIR/benchmark_cuda_fixture.patch"; then
  echo "fixture parser patch neither applies nor is already applied" >&2
  exit 1
fi

nix develop "$CHECKOUT" --command bash -c '
  set -euo pipefail
  cd "$1"
  cuda_home=$(dirname "$(dirname "$(readlink -f "$(command -v nvcc)")")")
  env -u NIX_ENFORCE_NO_NATIVE make -C c cuda-test \
    CUDA_HOME="$cuda_home" CUDA_ARCH=sm_120
  env -u NIX_ENFORCE_NO_NATIVE make -C c glm \
    CUDA=1 CUDA_HOME="$cuda_home" CUDA_ARCH=sm_120 ARCH=native
' bash "$CHECKOUT"

echo "Colibri $REVISION built at $CHECKOUT/c/glm"
