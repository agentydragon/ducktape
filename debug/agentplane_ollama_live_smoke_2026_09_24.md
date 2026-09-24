# Agentplane testing: Ollama live smoke (2026-09-24)

Scope: merged PR #7890, deployed `agentplane-testing`, real Agentplane sessions and harness turns. The manual driver is `//agentplane/acceptance:test_ollama_routes`; each cell uses an isolated Sandbox and requests a shell tool call. A passing cell requires the runner's recorded tool output to contain `OLLAMA_SMOKE_TOOL_OK` and `/state/work`.

## Observed cells

| Harness | Route                               | Result                                                                                                                                                                             | Session/thread                                                                       | Bazel invocation                       |
| ------- | ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | -------------------------------------- |
| Claude  | `ollama/oai-chat/gpt-oss-20b-128k`  | Failed before tool use: `TURN_STATUS_FAILED`, `ECONNREFUSED` diagnostic. No recorded tool output.                                                                                  | `4f20c599-fdef-4f66-b193-f5863bc08f51`, turn `turn-7daf2a52119f410aaa1df6b8995c1246` | `7a499796-40e2-4434-824e-cadfe4b4c008` |
| Codex   | `ollama/oai-chat/gpt-oss-20b-128k`  | Inference completed; no recorded tool output. Final answer says it lacks permission to execute shell commands.                                                                     | `fb22dbf5-a547-468e-9ffc-60314f276200`                                               | `f3ce474c-6df0-44c5-9374-a32bbc106672` |
| Codex   | `ollama/olm-chat/gpt-oss-20b-128k`  | Failed before tool use: Ollama HTTP 400 says `think` must be a boolean or `high`, `medium`, `low`, or `max`.                                                                       | turn `01a0d57e-12d8-7f62-b177-1a58ec7d1041`                                          | `ecdccd6b-09ab-4937-bb00-ef51a1b8fdc1` |
| Codex   | `ollama/oai-chat/gpt-oss-120b-128k` | No turn result after more than 300 seconds. Ollama remained in `load_tensors` / `llm server loading model`; the test was interrupted at 410 seconds. No tool or generation result. | Sandbox `accept-ollama-smoke-w2c6l`; no terminal turn id captured                    | `5f44c05d-04b5-4f9a-b091-59d05f745795` |

Four distinct cells were attempted: zero tool-call passes, three failed smoke checks (two failed turns and one completed turn without tools), and one interrupted loading turn. The remaining 20 cells were not attempted. The first three Sandboxes were deleted by fixture teardown. The interrupted test's Sandbox was also gone when checked immediately after pytest exited. The namespace had no `accept-ollama-smoke-*` Sandbox after the run.

## Complete route matrix

`F` is a failed turn or completed turn without the required tool output; `I` is the interrupted 120B load; `—` means no inference attempt was made. Every route below was offered by deployed Agentplane configuration, but offer is not execution proof.

| Model/context           | Wire     | Claude                | Codex                 |
| ----------------------- | -------- | --------------------- | --------------------- |
| gpt-oss-20b-128k        | oai-chat | F: connection refused | F: inference, no tool |
| gpt-oss-20b-128k        | olm-chat | —                     | F: Ollama 400 `think` |
| gpt-oss-120b-128k       | oai-chat | —                     | I: 120B loading >300s |
| gpt-oss-120b-128k       | olm-chat | —                     | —                     |
| gemma4-31b-it-q8_0-128k | oai-chat | —                     | —                     |
| gemma4-31b-it-q8_0-128k | olm-chat | —                     | —                     |
| gpt-oss-20b-256k        | oai-chat | —                     | —                     |
| gpt-oss-20b-256k        | olm-chat | —                     | —                     |
| gpt-oss-20b-512k        | oai-chat | —                     | —                     |
| gpt-oss-20b-512k        | olm-chat | —                     | —                     |
| gpt-oss-20b-1m          | oai-chat | —                     | —                     |
| gpt-oss-20b-1m          | olm-chat | —                     | —                     |

The remaining cells were left unattempted because Ollama continued loading the canceled 120B request. No model-level conclusion is drawn for Gemma, the larger 20B contexts, the untried Claude cells, or the untried native-wire cells.

## Path evidence and limits

- The Claude turn opened successfully, then failed in the harness with `API Error: Connection refused — a firewall or proxy may be blocking it (ECONNREFUSED)`. There was no matching decision for its Sandbox in the sampled application egress log and no Ollama generation request at that time. This locates the observed failure before the application egress decision log, but does not by itself identify the refused socket.
- Codex's Sandbox generated allowed application egress decisions for POSTs to `agentplane-llm-ingress:8080` at 22:12:51 and 22:13:33-34 UTC. Ollama logged a 200 response to `/v1/responses` at 22:13:33, the first call taking 41.7 seconds during model loading; later responses were under half a second. This proves one full model path worked. The application egress decision is not itself a Cilium kernel verdict.
- Live Cilium policies in `agentplane-testing` are Valid and select the relevant Pod labels. The runner policy allows TCP 8888 to `agentplane-egress`; the egress policy allows runner ingress on 8888 and TCP 8080 to `agentplane-llm-ingress`; the ingress policy allows egress ingress on 8080 and TCP 4000 to LiteLLM. Live services had endpoints at each hop, including Ollama `10.244.5.13:11434`. There were no namespaced CNPs in `litellm` or `ollama`; the clusterwide `sandbox-force-proxy-egress` policy selects `claude-sandbox`, not `agentplane-testing`. These observations make a missing allow rule on the nominal path unlikely; they do not exclude an unobserved Cilium drop or a separate direct path taken by Claude.
- Ollama logged a GPU discovery watchdog timeout at 22:10:45 UTC, but the first Claude failure was a connection refusal before a logged Ollama request. The later Codex call incurred a measured 41.7-second cold model load. Image pulling for the first Sandbox was a separate startup delay before any turn.
- The native Ollama wire accepted network traffic but rejected the `gpt-oss-20b` Codex request shape with HTTP 400. This is distinct from the Claude connection failure and the Codex OpenAI-wire tool refusal.
- At 22:17:36 UTC the 120B Codex request passed application egress. Ollama identified a 120B model and began loading tensors, with later `llm server loading model` entries. At the final read at 22:25:37 UTC, after test cancellation, it still reported loading, with no load-complete or generation response. This was an active backend load at the start, then an uncompleted load at the test's cutoff; health probes alone do not prove ongoing progress.
- The shared `Client.events(read_seconds=300)` bound is per received SSE frame, not an absolute turn deadline. It continued past 300 seconds because the connection remained open. The new manual driver now wraps `Agent.run` in an absolute `asyncio.timeout(300)` for subsequent runs. The existing 120B process was interrupted at 410 seconds and was not retried.
- The Codex runner config sets `approval_policy=never` and `sandbox_mode=danger-full-access`; no completed tool item appeared in its OpenAI-wire turn. The final response claimed no shell permission. We did not capture the upstream tool schema, so these observations do not distinguish a model refusal from tools omitted or malformed in the LiteLLM/Ollama Responses request. Similarly, the Claude failure occurred before an application egress decision, but the exact refused socket was not captured.

No service, policy, deployment, or model was changed. Recommended follow-up: inspect the Claude process's actual refused socket and sidecar readiness; capture sanitized Codex request tool metadata at the model ingress; correct the native Ollama `think` value; and investigate why 120B tensor loading did not finish within the turn budget before resuming the untouched matrix. These are recommendations, not implemented fixes.
