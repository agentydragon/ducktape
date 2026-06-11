#!/usr/bin/env bash
# Wrapper script for rust-analyzer's workspace.discoverConfig.
# Regenerates rust-project.json via Bazel and outputs it in the JSONL format
# that rust-analyzer expects.
#
# rust-analyzer sets cwd to the workspace root when invoking this script.
# Output: JSONL lines with kind=progress, kind=finished (or kind=error).
# The finished message embeds the full rust-project.json content inline.

set -euo pipefail

# The argument is JSON from rust-analyzer (DiscoverArgument), we don't need it
# since gen_rust_project generates the whole workspace at once.
# But we accept it for API compatibility.
ARG="${1:-}"

# rust-analyzer sets cwd to workspace root
PROJECT_FILE="$PWD/rust-project.json"

echo '{"kind":"progress","message":"Generating rust-project.json via Bazel..."}'

if ! bazelisk run @rules_rust//tools/rust_analyzer:gen_rust_project -- --config=nolint >/dev/null 2>&1; then
  echo '{"kind":"error","error":"bazelisk run @rules_rust//tools/rust_analyzer:gen_rust_project failed","source":"command"}'
  exit 1
fi

if [ ! -f "$PROJECT_FILE" ]; then
  echo "{\"kind\":\"error\",\"error\":\"rust-project.json not found at $PROJECT_FILE after generation\",\"source\":\"command\"}"
  exit 1
fi

# Embed the generated rust-project.json in the Finished message.
# jq -c compactifies; -s slurps the whole file as .[0].
# buildfile must be absolute — rust-analyzer panics on relative paths.
# The parent of buildfile becomes the root for crate path resolution.
BUILDFILE="$PWD/WORKSPACE"
cat "$PROJECT_FILE" | jq -c -s --arg bf "$BUILDFILE" '{
    kind: "finished",
    buildfile: $bf,
    project: .[0]
}'
