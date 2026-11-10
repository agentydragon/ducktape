# BuildBuddy gRPC read validation

Validated on 2026-10-30 against `app.buildbuddy.io:443` using the generated
`buildbuddy.service.BuildBuddyService` client from pinned BuildBuddy commit
`a07198b9707dc26cbce3827d807da6854a978638`. The client used the configured
HTTP CONNECT proxy, system certificate trust, and API-key metadata. No egress
extension was needed. This is a native gRPC endpoint; the existing JSON HTTP
endpoint uses BuildBuddy's protolet adapter, rather than Twirp.

Every migrated method returned gRPC status OK. Requests used invocation
`61acd48f-466b-47dd-a531-311a6f0c34af` and repository
`https://github.com/agentydragon/ducktape`; group and execution IDs came from
those read responses.

| Method                | Serialized response bytes |
| --------------------- | ------------------------: |
| GetInvocation         |                    63,954 |
| SearchInvocation      |                       388 |
| GetTarget             |                     7,754 |
| GetTargetHistory      |                 7,328,654 |
| GetTargetStats        |                       418 |
| GetTargetFlakeSamples |                         0 |
| GetInvocationStat     |                        45 |
| GetTrend              |                    10,495 |
| GetWorkflows          |                         0 |
| GetEventLogChunk      |                     6,465 |
| GetExecution          |                    12,018 |
| SearchExecution       |                       767 |
| GetCacheScoreCard     |                    24,540 |
| GetExecutionDownloads |                       111 |
| GetCacheMetadata      |                        24 |

The empty GetTargetFlakeSamples and GetWorkflows responses were successful RPCs,
not evidence that those methods returned useful records for this repository.
GetTargetHistory exceeds grpc-go's default 4 MiB receive limit; bbapi therefore
raises that limit. A local transport test also checks a 5 MiB response.

CancelExecutions, ExecuteWorkflow, and GetSuggestion were not probed and retain
their HTTP transport. Public API, raw BES, and artifact download routes also
remain HTTP. GetUser was probed separately and returned NotFound; successful
read probes do not establish access to every service method.
