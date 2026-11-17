#!/bin/bash
set -euo pipefail

hostname
test ! -e /run/current-system/sw/share/nix-ld
env -i HOME="$TEST_TMPDIR" USE_BAZEL_VERSION=9.2.0 /bin/bazel --version
