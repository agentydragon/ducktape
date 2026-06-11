---
name: buildbuddy_api
description: >
  Reference for querying BuildBuddy API. Use when investigating failed or slow
  CI builds, inspecting invocations by commit or branch, reading build or test logs,
  checking remote execution (RBE) details (exit codes, stderr, worker logs), analyzing
  cache hit/miss rates, downloading undeclared test outputs from RBE workers,
  bisecting test failures using target history to find the culprit commit range,
  viewing aggregated build statistics, triggering workflow re-runs, or getting AI
  analysis of build failures.
  Trigger when the user asks "why did this build fail", "show me the build log",
  "check RBE execution", "get test output from RBE", "what happened in this CI run",
  "check cache performance", "when did this test start failing", "find the commit
  that broke this test", "re-run CI", "trigger a workflow", "build stats by branch",
  "ask BuildBuddy about this failure", or any task that requires fetching data from
  BuildBuddy.
---

# BuildBuddy API

## Prerequisites

All commands require `BUILDBUDDY_API_KEY` to be set (session hook exports it automatically).

## Official `bb` CLI

The official BuildBuddy CLI (`bb`) handles build logs and execution details:

```bash
# View build log for an invocation (replaces bbapi invocation log)
bb view <invocation-id-or-url> [--lines=100000]

# Fetch cached execution response for a single execution ID
bb execution get <execution-id> [--output=json]

# Download raw blob from CAS by digest
bb download <digest>/<size> [--type=Action|Command]
```

## `bbapi` CLI

`bbapi` provides API query features not available in `bb`:

```bash
# Show invocation details (shows child invocation IDs for workflows)
bbapi invocation <invocation-id>

# List recent invocations (auto-detects repo from git remote)
bbapi invocation list [--repo URL] [--count N]

# Download test.log for a specific target (most common for debugging failures)
bbapi target log <invocation-id> <target-label-or-substring>

# List targets in an invocation (auto-resolves workflow/runner IDs to child)
bbapi target <invocation-id> [--filter SUBSTR] [--label LABEL]

# Show pass/fail/flake history for targets (auto-detects group_id)
bbapi target history [--repo URL] [--label LABEL] [--failures-only]

# Show flake statistics for targets (last 7 days)
bbapi target stats [--repo URL]

# Show flake samples for a specific target
bbapi target flakes <target-label> [--repo URL]

# List artifacts for an invocation
bbapi artifact list <invocation-id>

# Stream a matching artifact to stdout
bbapi artifact cat <invocation-id> <name-substring>

# Download a matching artifact to a file (defaults to artifact basename;
# override with -o/--output)
bbapi artifact download <invocation-id> <name-substring> [-o PATH]

# List invocation-level build tool logs such as Bazel command profiles
bbapi tool-log list <invocation-id>

# Stream an inline or bytestream-backed build tool log to stdout
bbapi tool-log cat <invocation-id> "critical path"

# Download a Bazel JSON profile emitted as a build tool log
bbapi tool-log download <invocation-id> command.profile.gz [-o PATH]

# Download logs from all child invocations of a workflow/runner invocation
bbapi tool-log download <runner-invocation-id> command.profile.gz --all -o profiles/

# List remote executions for an invocation
bbapi execution <invocation-id>

# Search remote executions across invocations
bbapi execution search <query>

# Aggregated invocation statistics (by branch, user, commit, etc.)
bbapi invocation stat [--agg-type branch|user|host|repo|commit|pattern] [--repo URL] [--limit N]

# AI analysis of a build/test failure (works on any invocation, unlike `bb ask`)
# Sends the last ~1000 lines of the build log (from first "ERROR:" onward, max 8KB)
# to OpenAI. Best for build errors with clear error messages in the log tail.
# Less useful for test timeouts (log tail may show unrelated warnings instead).
bbapi ask <invocation-id> [--prompt TEXT]

# List configured BuildBuddy workflows
bbapi workflow

# Trigger a workflow run (auto-detects workflow ID, branch, commit from git)
bbapi workflow run [--workflow-id ID] [--branch BRANCH] [--commit SHA] [--action NAME] [--async]

# Show cache scorecard (per-action hit/miss)
bbapi cache <invocation-id>

# Get metadata for a cached artifact by digest
bbapi cache metadata <digest> <size-bytes>

# Show build performance trends
bbapi trend [--days N] [--repo URL]
```

All `bbapi` commands support `--json` for raw JSON output.

## Investigating Failed CI Builds

Typical workflow for debugging a failed CI build:

```bash
# 1. Get invocation details — note the Child: line for workflow invocations
bbapi invocation <invocation-id>

# 2. Get the build log to see which tests failed
bb view <invocation-id>

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
- `bbapi artifact {list,cat,download}` and `bbapi target log` auto-resolve
  workflow invocations to their children — you can pass either the workflow
  or child ID
- `bbapi target` also auto-resolves workflow invocations to their children

### Artifact Name Matching

`bbapi artifact {cat,download}` and `bbapi target log` match against `"label/name"`:

- `"test_handlers"` matches `//x/gatelet/server/auth:test_handlers/test.log`
- `"test_handlers/test.xml"` matches the XML output specifically
- `"compositor/test_lifecycle"` matches `//mcp_infra/compositor:test_lifecycle/test.log`

When no match is found, the CLI prints available labels as hints.

### Build Tool Logs vs Test Artifacts

Bazel files attached to the invocation itself, such as `command.profile.gz`, are BES
`BuildToolLogs`, not test artifacts. Use `bbapi tool-log {list,cat,download}` for these
files. Use `bbapi artifact` only for test outputs like `test.log`, `test.xml`, and files
under `test.outputs/`. Build tool logs can be inline (`elapsed time`, `critical path`,
`process stats`) or bytestream-backed (`command.profile.gz`); `bbapi tool-log cat` handles
both.

For CI phase profiling, start with:

```bash
bb view <runner-or-child-invocation> --lines=200000
bbapi invocation <runner-invocation>   # shows child invocations
bbapi tool-log list <runner-invocation>
bbapi tool-log cat <child-invocation> "process stats"
bbapi tool-log download <runner-invocation> command.profile.gz --all -o profiles/
```

The runner log line `Syncing existing repo...` identifies a warm outer `bb remote` runner
workspace, but it does not by itself prove the inner Bazel analysis cache survived. Check
Bazel's package/configuration counts, elapsed time, `targetConfiguredCount`, and
`command.profile.gz`. Treat `targetConfiguredCount` as the configured graph size/result
count, not by itself proof that those configured targets were recomputed; profile markers,
`discarding analysis cache` warnings, and time-to-first-action are better recomputation
signals.

## Bisecting Test Failures with Target History

When a test is failing and you need to find the commit that broke it, use target
history instead of `git bisect` — BuildBuddy already has all the results:

```bash
# 1. Check recent pass/fail history for the target (failures only)
bbapi target history --failures-only --label //path/to:test_target

# 2. Identify the transition point (last pass → first fail)
#    The output shows invocation IDs and commit SHAs for each run

# 3. Narrow to the commit range
git log --oneline <last-pass-commit>..<first-fail-commit>

# 4. Read the test log from the first failing invocation
bbapi target log <first-failing-invocation-id> test_target
```

This is much faster than `git bisect` because it doesn't require re-running the
test — the results are already in BuildBuddy's database.

## Diagnosing Executor Environments

Use `bb execute` to run one-off commands directly on a BuildBuddy executor to probe the container image, check installed tools, verify library paths, etc. Useful when builds fail due to missing dependencies or environment issues.

```bash
# Probe what's in a container image
bb execute \
  -exec_properties=container-image=docker://ghcr.io/agentydragon/rbe-worker:nix-devtools \
  -- bash -c 'gcc --version; python3 --version; ldd --version | head -1'

# Check if a specific library exists
bb execute \
  -exec_properties=container-image=docker://ghcr.io/agentydragon/rbe-worker:latest \
  -- bash -c 'find / -name "libstdc++.so*" 2>/dev/null'

# Test pip wheel compatibility (manylinux tags)
bb execute \
  -exec_properties=container-image=docker://ghcr.io/agentydragon/rbe-worker:latest \
  -- bash -c 'python3 -m pip install --dry-run some-package==1.0'
```

`bb execute` runs on the default executor image (Ubuntu 16.04, glibc 2.23) unless you specify `-exec_properties=container-image=...`. Use `-exec_properties=workload-isolation-type=firecracker` to test with Firecracker isolation.

### Interactive debugging with `bb ssh`

For interactive debugging, start an SSH server on a BuildBuddy executor:

```bash
# On the executor (in one terminal)
bb ssh-server my-debug-session

# Connect from your machine (in another terminal)
bb ssh my-debug-session
```

This gives a full shell on the executor for investigating build failures, inspecting the filesystem, testing commands interactively, etc.

## BuildBuddy Concepts

**Group ID**: BuildBuddy's organization identifier (e.g., `GR7963402054611859571`).
Scopes API queries to the org's data. Auto-detected by `bbapi` from a recent invocation's
ACL — no manual configuration needed.

**Workflow ID**: For repos using `buildbuddy.yaml` + GitHub app, workflow IDs are synthetic:
`WF#GitRepository:{group_id}:{repo_url}`. The `bbapi workflow run` command auto-constructs
this from the detected group_id and repo URL. `GetWorkflows` returns empty for these repos
(it only lists explicitly created workflows).

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
