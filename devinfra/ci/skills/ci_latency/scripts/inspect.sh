#!/usr/bin/env bash
# Metadata, a selected Actions job log, and its exact inner Bazel invocation.
set -euo pipefail
if [[ $# != 4 ]]; then
  echo "Usage: $0 OWNER/REPO JOB_ID BAZEL_INVOCATION_ID NEW_OUTPUT_DIRECTORY" >&2
  exit 2
fi
repo=$1
job=$2
invocation=$3
out=$4
mkdir "$out"
gh api "repos/$repo/actions/workflows?per_page=100" --paginate --slurp >"$out/workflows.json"
gh api "repos/$repo/code-scanning/default-setup" >"$out/codeql.json"
# Rulesets, rather than only the legacy branch-protection endpoint.
gh api "repos/$repo/rules/branches/devel" >"$out/branch-rules.json"
gh api --allow-escape-sequences "repos/$repo/actions/jobs/$job/logs" >"$out/job.log"
timeout 60 bbapi invocation "$invocation" --json >"$out/invocation.json"
timeout 60 bbapi tool-log cat "$invocation" 'critical path' >"$out/critical-path.txt"
# Logs are local evidence: inspect/redact before publishing anything from them.
