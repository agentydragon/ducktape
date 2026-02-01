# BuildBuddy API: Documented and Undocumented Endpoints

Notes from probing the BuildBuddy API (2026-02-01). The
[official docs](https://www.buildbuddy.io/docs/enterprise-api/) cover only 9
endpoints. The BuildBuddy backend is
[open source](https://github.com/buildbuddy-io/buildbuddy) and exposes **120+
internal RPC methods** via `/rpc/BuildBuddyService/`, most of which work with a
standard API key.

## Authentication

All endpoints accept `x-buildbuddy-api-key` as an HTTP header. Without it,
requests return 403.

## Endpoint Surface (from source)

The HTTP mux in `server/libmain/libmain.go` registers:

| Path                            | Auth | Description                           |
| ------------------------------- | ---- | ------------------------------------- |
| `/api/v1/*`                     | yes  | Public API (9 RPCs, protolet handler) |
| `/api/v1/GetFile`               | yes  | Separate handler (streaming download) |
| `/api/v1/metrics`               | yes  | Prometheus metrics export             |
| `/rpc/BuildBuddyService/*`      | yes  | Internal service (120+ RPCs)          |
| `/file/download`                | yes  | Blob/artifact download (GET)          |
| `/file/view`                    | yes  | Blob/artifact view (GET)              |
| `/healthz`                      | no   | Liveness probe                        |
| `/readyz`                       | no   | Readiness probe                       |
| `/login/`, `/auth/`, `/logout/` | no   | OAuth flow                            |

## Public API v1 (`/api/v1/`)

Defined in `proto/api/v1/service.proto`. All use POST with JSON body.

| Method             | Description                              |
| ------------------ | ---------------------------------------- |
| `GetInvocation`    | Fetch by invocation ID or commit SHA     |
| `GetLog`           | Build log (stderr) for an invocation     |
| `GetTarget`        | Targets with label, status, rule type    |
| `GetAction`        | Actions for a target (minimal metadata)  |
| `GetFile`          | Stream-download a blob by bytestream URI |
| `DeleteFile`       | Delete a file from cache                 |
| `ExecuteWorkflow`  | Trigger a BuildBuddy workflow            |
| `Run`              | Remote bazel run                         |
| `CreateUserApiKey` | Create a user-scoped API key             |

### `GetInvocation`

```bash
# By invocation ID
curl -s -X POST -H "Content-Type: application/json" \
  -H "x-buildbuddy-api-key: $KEY" \
  -d '{"selector":{"invocation_id":"<UUID>"}}' \
  "https://app.buildbuddy.io/api/v1/GetInvocation"

# By commit SHA (returns ALL invocations for that commit)
curl -s -X POST -H "Content-Type: application/json" \
  -H "x-buildbuddy-api-key: $KEY" \
  -d '{"selector":{"commit_sha":"<sha>"}}' \
  "https://app.buildbuddy.io/api/v1/GetInvocation"
```

Response fields: `success`, `user`, `host`, `command`, `pattern`,
`actionCount`, `durationUsec`, `repoUrl`, `commitSha`, `branchName`,
`bazelExitCode`, `invocationStatus`, timestamps.

### `GetTarget`

```bash
curl -s -X POST -H "Content-Type: application/json" \
  -H "x-buildbuddy-api-key: $KEY" \
  -d '{"selector":{"invocation_id":"<UUID>"}}' \
  "https://app.buildbuddy.io/api/v1/GetTarget"
```

Returns per-target: `label`, `status` (`BUILT`), `ruleType`, `language`.

### `GetFile`

Downloads a blob by bytestream URI. Returns raw bytes (not JSON):

```bash
curl -s -X POST -H "Content-Type: application/json" \
  -H "x-buildbuddy-api-key: $KEY" \
  -d '{"uri":"bytestream://remote.buildbuddy.io/blobs/<sha256>/<size>"}' \
  "https://app.buildbuddy.io/api/v1/GetFile" > output.bin
```

## Internal RPC Service (`/rpc/BuildBuddyService/`)

Defined in `proto/buildbuddy_service.proto`. These are the same RPCs the web UI
uses. All use POST with JSON body. The API key works for most endpoints; a few
(like `GetUsage`) return 403, probably requiring org-level permissions.

### Verified Working

| Method              | Example request                                                       |
| ------------------- | --------------------------------------------------------------------- |
| `SearchInvocation`  | `{"query":{"repo_url":"https://github.com/..."},"count":10}`          |
| `GetCacheScoreCard` | `{"invocation_id":"<UUID>"}`                                          |
| `GetInvocationStat` | `{"aggregation_type":"DATE_TYPE","bucket_size_micros":"86400000000"}` |
| `GetTrend`          | Requires `group_id` (returns `InvalidArgument` without it)            |

### `SearchInvocation` (key endpoint not in public API)

Lists invocations by repo, branch, user, command, etc. — the missing "list"
endpoint:

```bash
curl -s -X POST -H "Content-Type: application/json" \
  -H "x-buildbuddy-api-key: $KEY" \
  -d '{"query":{"repo_url":"https://github.com/user/repo"},"count":5}' \
  "https://app.buildbuddy.io/rpc/BuildBuddyService/SearchInvocation"
```

Query fields (from `ExecutionQuery` proto): `invocation_user`,
`invocation_host`, `repo_url`, `commit_sha`, `role`, `branch_name`, `command`,
`pattern`, `tags`, `updated_after`, `updated_before`.

### `GetCacheScoreCard`

Per-action cache hit/miss details with digests, durations, transfer sizes:

```bash
curl -s -X POST -H "Content-Type: application/json" \
  -H "x-buildbuddy-api-key: $KEY" \
  -d '{"invocation_id":"<UUID>"}' \
  "https://app.buildbuddy.io/rpc/BuildBuddyService/GetCacheScoreCard"
```

### All Internal RPCs (from proto)

<details>
<summary>Full list of 120+ RPC methods</summary>

**Invocation**:
`GetInvocation`, `SearchInvocation`, `GetInvocationStat`,
`UpdateInvocation`, `DeleteInvocation`, `CancelExecutions`,
`GetInvocationOwner`, `GetInvocationFilterSuggestions`

**Stats/Trends**:
`GetTrend`, `GetStatHeatmap`, `GetStatDrilldown`, `GetTargetTrends`

**Execution**:
`GetExecution`, `WaitExecution`, `GetExecutionNodes`, `SearchExecution`

**Cache**:
`GetCacheScoreCard`, `GetCacheMetadata`, `GetTreeDirectorySizes`

**Targets**:
`GetTarget`, `GetTargetHistory`, `GetTargetStats`, `GetDailyTargetStats`,
`GetTargetFlakeSamples`

**Config/Zip**:
`GetZipManifest`, `GetBazelConfig`

**Users/Groups**:
`CreateUser`, `GetUser`, `GetGroup`, `GetGroupUsers`, `UpdateGroupUsers`,
`JoinGroup`, `CreateGroup`, `UpdateGroup`, `SetGroupStatus`

**User Lists**:
`GetUserLists`, `GetUserList`, `CreateUserList`, `DeleteUserList`,
`UpdateUserList`, `UpdateUserListMembership`

**API Keys**:
`GetApiKeys`, `GetApiKey`, `CreateApiKey`, `UpdateApiKey`, `DeleteApiKey`,
`CreateImpersonationApiKey`, `GetUserApiKeys`, `GetUserApiKey`,
`CreateUserApiKey`, `UpdateUserApiKey`, `DeleteUserApiKey`

**Workflows**:
`DeleteWorkflow`, `GetWorkflows`, `ExecuteWorkflow`, `GetRepos`,
`GetWorkflowHistory`, `InvalidateSnapshot`, `InvalidateAllSnapshotsForRepo`

**Workspaces**:
`GetWorkspace`, `SaveWorkspace`, `GetWorkspaceDirectory`, `GetWorkspaceFile`

**GitHub** (40+ methods):
Account linking, app installations, repositories, pull requests

**Secrets/Encryption**:
`ListSecrets`, `UpdateSecret`, `DeleteSecret`, `GetPublicKey`,
`SetEncryptionConfig`, `GetEncryptionConfig`

**Other**:
`Run`, `GetEventLogChunk`, `GetEventLog`, `GetUsage`, `GetNamespace`,
`RemoveNamespace`, `ModifyNamespace`, `ApplyBucket`, `GetSuggestion`,
`Search`, `GetAuditLogs`, `CreateRepo`, `GetGCPProject`, IP rules
management

</details>

### Access Restrictions

Some internal endpoints return 403 even with a valid API key:

| Method     | Error                    |
| ---------- | ------------------------ |
| `GetUsage` | `PermissionDenied` (403) |

These likely require org-admin or specific permission levels.

## `/file/download` and `/file/view` (GET)

Undocumented HTTP GET handlers for downloading/viewing artifacts. Both accept
the same parameters; `/file/view` presumably sets `Content-Disposition: inline`
while `/file/download` sets `attachment`.

### Mode 1: `artifact=raw_json`

Downloads the full Build Event Stream (BES) as a JSON array.

```
GET /file/download?invocation_id=<UUID>&artifact=raw_json
```

- Returns: JSON array of BES events (~1-2 MB for a typical `bazel build //...`)
- `request_context` parameter (base64 blob the UI sends): **not required**
- The only recognized `artifact` value is `raw_json` — all others return
  `FailedPrecondition: Unrecognized artifact "<name>" requested`

### Mode 2: `bytestream_url=<encoded-URI>`

Downloads a specific blob from the remote cache by its bytestream URI.

```
GET /file/download?bytestream_url=<url-encoded bytestream URI>
```

- `invocation_id` is **optional** when using `bytestream_url`
- Returns: raw blob bytes (e.g., gzipped profile data)
- Bytestream URI format:
  `bytestream://remote.buildbuddy.io/blobs/<sha256>/<size>`

## Downloading a Build Profile (Flame Chart)

The profile is not directly exposed by any named artifact. Two-step process:

1. **Fetch the BES event stream:**

   ```bash
   curl -s -H "x-buildbuddy-api-key: $KEY" \
     "https://app.buildbuddy.io/file/download?invocation_id=$ID&artifact=raw_json" \
     > events.json
   ```

2. **Extract the profile bytestream URI** from the `buildToolLogs` event:

   ```bash
   python3 -c "
   import json
   events = json.load(open('events.json'))
   for e in events:
       if 'buildToolLogs' in e.get('id', {}):
           for log in e['buildToolLogs']['log']:
               if log['name'] == 'command.profile.gz':
                   print(log['uri'])
   "
   ```

3. **Download the profile blob:**

   ```bash
   PROFILE_URI="bytestream://remote.buildbuddy.io/blobs/..."
   curl -s -H "x-buildbuddy-api-key: $KEY" \
     "https://app.buildbuddy.io/file/download?bytestream_url=$(python3 -c \
       "import urllib.parse; print(urllib.parse.quote('$PROFILE_URI', safe=''))")" \
     > profile.gz
   ```

4. **View it:** Load `profile.gz` in `chrome://tracing` or
   [ui.perfetto.dev](https://ui.perfetto.dev).

## BES Event Data Available via `raw_json`

The `buildToolLogs` event contains:

| `name`               | Format          | Description                              |
| -------------------- | --------------- | ---------------------------------------- |
| `elapsed time`       | inline (base64) | Total wall time in seconds               |
| `critical path`      | inline (base64) | Critical path breakdown with percentages |
| `process stats`      | inline (base64) | Action execution summary                 |
| `command.profile.gz` | bytestream URI  | Chrome Trace Format profile (gzipped)    |

The `buildMetrics` event contains `actionSummary` with per-mnemonic action
counts and timing. The `namedSet` events contain bytestream URIs for all build
output files.

## Practical Recipes

### List recent invocations for this repo

```bash
curl -s -X POST -H "Content-Type: application/json" \
  -H "x-buildbuddy-api-key: $KEY" \
  -d '{"query":{"repo_url":"https://github.com/agentydragon/ducktape"},"count":10}' \
  "https://app.buildbuddy.io/rpc/BuildBuddyService/SearchInvocation"
```

### Get cache hit rate for an invocation

```bash
curl -s -X POST -H "Content-Type: application/json" \
  -H "x-buildbuddy-api-key: $KEY" \
  -d '{"invocation_id":"<UUID>"}' \
  "https://app.buildbuddy.io/rpc/BuildBuddyService/GetCacheScoreCard"
```

### Download profile for the slowest recent build

```bash
# 1. Find the invocation
ID=$(curl -s -X POST -H "Content-Type: application/json" \
  -H "x-buildbuddy-api-key: $KEY" \
  -d '{"query":{"repo_url":"https://github.com/agentydragon/ducktape","command":"build"},"count":5}' \
  "https://app.buildbuddy.io/rpc/BuildBuddyService/SearchInvocation" \
  | python3 -c "import sys,json; invs=json.load(sys.stdin)['invocation']; print(max(invs, key=lambda i: int(i.get('durationUsec','0')))['invocationId'])")

# 2. Get BES and extract profile URI
PROFILE_URI=$(curl -s -H "x-buildbuddy-api-key: $KEY" \
  "https://app.buildbuddy.io/file/download?invocation_id=$ID&artifact=raw_json" \
  | python3 -c "
import sys,json
for e in json.load(sys.stdin):
    for log in e.get('buildToolLogs',{}).get('log',[]):
        if log.get('name')=='command.profile.gz':
            print(log['uri']); break
")

# 3. Download
curl -s -H "x-buildbuddy-api-key: $KEY" \
  "https://app.buildbuddy.io/file/download?bytestream_url=$(python3 -c \
    "import urllib.parse; print(urllib.parse.quote('$PROFILE_URI', safe=''))")" \
  > profile.gz
```
