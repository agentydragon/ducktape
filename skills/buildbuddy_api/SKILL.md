---
name: buildbuddy_api
description: >
  Reference for querying the BuildBuddy API. Use when investigating failed or slow
  CI builds, inspecting invocations by commit or branch, reading build or test logs,
  checking remote execution (RBE) details (exit codes, stderr, worker logs), analyzing
  cache hit/miss rates, or downloading undeclared test outputs from RBE workers.
  Trigger when the user asks "why did this build fail", "show me the build log",
  "check RBE execution", "get test output from RBE", "what happened in this CI run",
  "check cache performance", or any task that requires fetching data from BuildBuddy.
allowed-tools: Bash
---

# BuildBuddy API

## Prerequisites

All commands require `BUILDBUDDY_API_KEY` to be set (session hook exports it automatically).

## CLI (`bbapi`)

If `bbapi` is in PATH, prefer it over raw API calls:

```bash
# Show invocation details (shows child invocation IDs for workflows)
bbapi invocation <invocation-id>

# List recent invocations (auto-detects repo from git remote)
bbapi invocation list [--repo URL] [--count N]

# Print build log (shows test pass/fail summary with test.log paths)
bbapi invocation log <invocation-id>

# Download test.log for a specific target (most common for debugging failures)
bbapi target log <invocation-id> <target-label-or-substring>

# List targets in an invocation
bbapi target <invocation-id> [--filter SUBSTR] [--label LABEL]

# Show pass/fail/flake history for targets
bbapi target history <target-label>

# List artifacts, or download one by name match
bbapi artifact <invocation-id> [name-substring]

# List remote executions for an invocation
bbapi execution <invocation-id>

# Search remote executions across invocations
bbapi execution search <query>

# Show cache scorecard (per-action hit/miss)
bbapi cache <invocation-id>

# Get metadata for a cached artifact by digest
bbapi cache metadata <digest> <size-bytes>

# Show build performance trends
bbapi trend [--days N] [--repo URL]
```

All commands support `--json` for raw JSON output.

## Investigating Failed CI Builds

Typical workflow for debugging a failed CI build:

```bash
# 1. Get invocation details — note the Child: line for workflow invocations
bbapi invocation <invocation-id>

# 2. Get the build log to see which tests failed
bbapi invocation log <invocation-id>

# 3. Download the test.log for a specific failed target
#    Works with both workflow and child invocation IDs (auto-resolves)
bbapi target log <invocation-id> <target-substring>
#    Example: bbapi target log 870a5be1-c296-4792-8c8a-77def20b2dcc test_handlers
```

### Workflow vs Child Invocations

BuildBuddy CI runs use **workflow invocations** that spawn **child invocations**.
The workflow invocation (command: `workflow run`) is a wrapper; the child
invocation contains the actual `bazel test` results, targets, and artifacts.

- `bbapi invocation` shows `Child: <child-id>` for workflow invocations
- `bbapi artifact` and `bbapi target log` auto-resolve workflow invocations
  to their children — you can pass either the workflow or child ID
- `bbapi target` does NOT auto-resolve (uses the BuildBuddy `GetTarget` RPC
  which requires the exact invocation ID)

### Artifact Name Matching

`bbapi artifact` and `bbapi target log` match against `"label/name"`:

- `"test_handlers"` matches `//x/gatelet/server/auth:test_handlers/test.log`
- `"test_handlers/test.xml"` matches the XML output specifically
- `"compositor/test_lifecycle"` matches `//mcp_infra/compositor:test_lifecycle/test.log`

When no match is found, the CLI prints available labels as hints.

## Raw API Fallback

If `bbapi` is not available, use the Twirp JSON API at `app.buildbuddy.io` directly
with curl. Read <devinfra/buildbuddy_cli/client.go> for how the CLI talks to the API
(Twirp JSON over HTTP). The API key comes from `BUILDBUDDY_API_KEY` env var, or
parse it from `~/.config/bazel/buildbuddy.bazelrc` (`x-buildbuddy-api-key=...`).

Proto definitions for request/response schemas:

- <https://github.com/buildbuddy-io/buildbuddy/blob/master/proto/buildbuddy_service.proto> (internal, ~70 RPCs)
- <https://github.com/buildbuddy-io/buildbuddy/blob/master/proto/api/v1/service.proto> (public, 9 endpoints)

## Known Limitations

**Fork PRs don't have BuildBuddy invocations.** GitHub Actions does not pass
`BUILDBUDDY_API_KEY` to workflows triggered by fork pull requests (head repo !=
base repo). As a result, `bazel-check` and `bazel-test` are skipped entirely on
fork PRs (see [#787](https://github.com/agentydragon/ducktape/issues/787)).
When investigating a failed fork PR, BuildBuddy has no record of the run — check
GitHub Actions logs directly instead.
