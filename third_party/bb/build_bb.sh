#!/usr/bin/env bash
# Builds the patched bb CLI: upstream cli-v5.0.387 + pr13067.patch.
# Usage: build_bb.sh <buildbuddy-src-dir> <output-path>
set -euo pipefail

src=$1
out=$2
patch_dir=$(cd "$(dirname "$0")" && pwd)

# cli-v5.0.387 — must match the checkout the workflow (or a local caller) made.
expected_sha=e85d89e0bf52040c222424f9ca8b3ce4649116c1
actual_sha=$(git -C "$src" rev-parse HEAD)
if [[ "$actual_sha" != "$expected_sha" ]]; then
  echo "source at $actual_sha, expected cli-v5.0.387 ($expected_sha)" >&2
  exit 1
fi

git -C "$src" apply "$patch_dir/pr13067.patch"

# The version genrule reads STABLE_CLI_VERSION_TAG from workspace status
# (cli/version/BUILD); every other stamped genrule has an unset-value
# fallback, so a minimal status command is sufficient and makes the binary
# self-identify as patched.
status_script=$(mktemp)
printf '#!/usr/bin/env bash\necho "STABLE_CLI_VERSION_TAG 5.0.387-pr13067"\n' >"$status_script"
chmod +x "$status_script"

# Mirrors the linux-amd64 leg of upstream's .github/workflows/release-cli.yaml;
# --config=static is //platforms:linux_x86_64_musl, so the output is fully static.
(cd "$src" && bazelisk build \
  --workspace_status_command="$status_script" \
  --stamp \
  --compilation_mode=opt \
  --strip=always \
  --config=static \
  //cli/cmd/bb)

install -m 0755 "$src/bazel-bin/cli/cmd/bb/bb_/bb" "$out"
"$out" version
