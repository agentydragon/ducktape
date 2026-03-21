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

# BuildBuddy API Reference

BuildBuddy's backend is open-source (<https://github.com/buildbuddy-io/buildbuddy>).
Proto definitions: `proto/buildbuddy_service.proto` (internal, ~70 RPCs) and
`proto/api/v1/service.proto` (public, 9 endpoints).

## Session Setup

Run once before any API calls:

```bash
KEY="$(grep -oP 'x-buildbuddy-api-key=\K.*' ~/.config/bazel/buildbuddy.bazelrc)"
BB="https://app.buildbuddy.io"
bb_get() { curl -s -H "x-buildbuddy-api-key: $KEY" "$@"; }
bb()     { bb_get -X POST -H "Content-Type: application/json" "$@"; }
```

## Common Recipes

```bash
# List recent invocations for this repo
bb -d '{"query":{"repo_url":"https://github.com/agentydragon/ducktape"},"count":10}' \
  "$BB/rpc/BuildBuddyService/SearchInvocation"

# Get invocation by ID (public API)
bb -d '{"selector":{"invocation_id":"<UUID>"}}' "$BB/api/v1/GetInvocation"

# Get build log (chunked; increase min_lines for larger logs)
bb -d '{"invocation_id":"<UUID>","min_lines":500}' \
  "$BB/rpc/BuildBuddyService/GetEventLogChunk"

# Get remote executions for an invocation
bb -d '{"execution_lookup":{"invocation_id":"<UUID>"},"inline_execute_response":true}' \
  "$BB/rpc/BuildBuddyService/GetExecution"

# Get cache scorecard (per-action hit/miss, durations, sizes)
bb -d '{"invocation_id":"<UUID>"}' "$BB/rpc/BuildBuddyService/GetCacheScoreCard"

# Download BES event stream (JSON) — contains all build events
bb_get "$BB/file/download?invocation_id=<UUID>&artifact=raw_json"

# Download build profile (open in chrome://tracing or ui.perfetto.dev)
bb_get "$BB/file/download?invocation_id=<UUID>&artifact=execution_profile&execution_id=<EXEC_ID>"
```

## Downloading Undeclared Test Outputs from RBE

Tests running on RBE write undeclared outputs (`TEST_UNDECLARED_OUTPUTS_DIR`) to the
remote worker. These are uploaded as BES artifacts and downloadable via `/file/download`.

```bash
INVOCATION="<UUID>"

# 1. Fetch the BES event stream
bb_get "$BB/file/download?invocation_id=$INVOCATION&artifact=raw_json" > bes.json

# 2. List all test output files
jq -r '.[].testResult.testActionOutput[]?.name' bes.json | sort

# 3. Download one file by name
URI=$(jq -r '.[].testResult.testActionOutput[]? | select(.name | contains("proxy.log")) | .uri' bes.json | head -1)
bb_get "$BB/file/download?bytestream_url=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1],safe=''))" "$URI")"
```

## Other Endpoints

| Path                | Method | Notes                             |
| ------------------- | ------ | --------------------------------- |
| `/api/v1/GetTarget` | POST   | Targets with label, status, rule type |
| `/api/v1/GetLog`    | POST   | Build stderr                      |
| `/api/v1/GetFile`   | POST   | Download blob by bytestream URI   |

For the full ~70 internal RPCs, see `proto/buildbuddy_service.proto` in the
[BuildBuddy repo](https://github.com/buildbuddy-io/buildbuddy).
