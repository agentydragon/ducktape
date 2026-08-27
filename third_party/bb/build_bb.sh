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

# Emit every STABLE_ key a genrule consumes (cli/version and server/version
# BUILDs). server/version's genrules run under set -e -o pipefail without a
# grep fallback, so a missing key fails the build rather than defaulting.
status_script=$(mktemp)
cat >"$status_script" <<EOF
#!/usr/bin/env bash
echo "STABLE_CLI_VERSION_TAG 5.0.387-pr13067"
echo "STABLE_VERSION_TAG 5.0.387-pr13067"
echo "STABLE_COMMIT_SHA $expected_sha"
EOF
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
