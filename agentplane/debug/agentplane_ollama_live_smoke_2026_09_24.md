# Agentplane testing: Ollama live smoke (2026-09-24)

Scope: merged PR #7890, deployed `agentplane-testing`, real Agentplane sessions and harness turns. The manual driver is `//agentplane/acceptance:test_ollama_routes`; each cell uses an isolated Sandbox and requests a shell tool call. A passing cell requires the runner's recorded tool output to contain `OLLAMA_SMOKE_TOOL_OK` and `/state/work`.

## Observed cells

| Harness | Route                                     | Result                                                                                                                                                                             | Session/thread                                                                       | Bazel invocation                       |
| ------- | ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | -------------------------------------- |
| Claude  | `ollama/oai-chat/gpt-oss-20b-128k`        | Failed before tool use: `TURN_STATUS_FAILED`, `ECONNREFUSED` diagnostic. No recorded tool output.                                                                                  | `4f20c599-fdef-4f66-b193-f5863bc08f51`, turn `turn-7daf2a52119f410aaa1df6b8995c1246` | `7a499796-40e2-4434-824e-cadfe4b4c008` |
| Codex   | `ollama/oai-chat/gpt-oss-20b-128k`        | Inference completed; no recorded tool output. Final answer says it lacks permission to execute shell commands.                                                                     | `fb22dbf5-a547-468e-9ffc-60314f276200`                                               | `f3ce474c-6df0-44c5-9374-a32bbc106672` |
| Codex   | `ollama/olm-chat/gpt-oss-20b-128k`        | Failed before tool use: Ollama HTTP 400 says `think` must be a boolean or `high`, `medium`, `low`, or `max`.                                                                       | turn `01a0d57e-12d8-7f62-b177-1a58ec7d1041`                                          | `ecdccd6b-09ab-4937-bb00-ef51a1b8fdc1` |
| Codex   | `ollama/oai-chat/gpt-oss-120b-128k`       | No turn result after more than 300 seconds. Ollama remained in `load_tensors` / `llm server loading model`; the test was interrupted at 410 seconds. No tool or generation result. | Sandbox `accept-ollama-smoke-w2c6l`; no terminal turn id captured                    | `5f44c05d-04b5-4f9a-b091-59d05f745795` |
| Claude  | `ollama/oai-chat/gpt-oss-20b-128k`        | Repeat passed: completed turn, recorded shell output `/state/work` and `OLLAMA_SMOKE_TOOL_OK`, correct final answer.                                                               | `a06ad295-c1ad-43be-9ec8-f63e6bd32c20`                                               | `81a9bbae-31d3-48af-aa57-45c44a73843a` |
| Codex   | `ollama/oai-chat/gpt-oss-20b-128k`        | Repeat passed: completed turn, native `commandExecution`, recorded shell output and correct final answer.                                                                          | `6a86a63f-bdbc-4eaf-8cd7-48a880b4f937`                                               | `b5dace53-b737-4a55-b785-8a64357278c1` |
| Claude  | `ollama/oai-chat/gemma4-31b-it-q8_0-128k` | Absolute turn timeout at 300 seconds while Gemma loaded; no inference/tool result at cutoff.                                                                                       | `85e0d0c0-629f-4246-a74d-19f2089e34bd`                                               | `1f3bed02-15d5-4c2d-bb43-c1f8631f3a37` |
| Claude  | `ollama/oai-chat/gemma4-31b-it-q8_0-128k` | Repeat passed: completed turn with recorded shell marker and `/state/work`, correct answer.                                                                                        | `533a63a2-4844-45e3-ab1f-326ae5c594fb`                                               | `d6e80ae8-c042-4305-b75d-45be9a9eac83` |
| Codex   | `ollama/oai-chat/gemma4-31b-it-q8_0-128k` | Passed: completed turn with native `commandExecution`, recorded shell marker and `/state/work`, correct answer.                                                                    | `ffeac5d3-d9c6-4336-9a32-ee7f4aef7682`                                               | `57086830-544d-4232-ad10-5436baeccf72` |

Nine live harness runs covered six distinct route/harness cells: four tool-call passes (20B and Gemma, both harnesses, OpenAI wire), three failed first checks (two failed turns and one completed turn without tools), and two cold-load-limited turns (one interrupted 120B, one absolute timeout Gemma). Eighteen route/harness variants remain unattempted, including Claude/120B; the optional larger-context grid is not necessary to establish the three-model/two-harness baseline. Every test Sandbox, including the timed-out/interrupt cases, was gone after its run. The passed repeats do not erase the first failures.

## Complete route matrix

`F→P` means first failed then passed with recorded tool output on repeat; `T→P` means cold-load timeout then tool pass; `F` is a failed turn without tool output; `I` is the interrupted 120B load; `—` means no inference attempt was made. Every route below was offered by deployed Agentplane configuration, but offer is not execution proof.

| Model/context           | Wire     | Claude                       | Codex                   |
| ----------------------- | -------- | ---------------------------- | ----------------------- |
| gpt-oss-20b-128k        | oai-chat | F→P: refused, then tool      | F→P: no tool, then tool |
| gpt-oss-20b-128k        | olm-chat | —                            | F: Ollama 400 `think`   |
| gpt-oss-120b-128k       | oai-chat | —                            | I: 120B loading >300s   |
| gpt-oss-120b-128k       | olm-chat | —                            | —                       |
| gemma4-31b-it-q8_0-128k | oai-chat | T→P: cold timeout, then tool | P: recorded tool        |
| gemma4-31b-it-q8_0-128k | olm-chat | —                            | —                       |
| gpt-oss-20b-256k        | oai-chat | —                            | —                       |
| gpt-oss-20b-256k        | olm-chat | —                            | —                       |
| gpt-oss-20b-512k        | oai-chat | —                            | —                       |
| gpt-oss-20b-512k        | olm-chat | —                            | —                       |
| gpt-oss-20b-1m          | oai-chat | —                            | —                       |
| gpt-oss-20b-1m          | olm-chat | —                            | —                       |

Most remaining cells were left unattempted because cold model loading repeatedly consumed or exceeded the bounded turn budget. The user's core three-model/two-harness baseline still lacks a 120B Claude result: repeating the 65GB cold load through a second harness would add cluster cost without distinguishing its already observed storage/timeout bottleneck. The larger contexts are optional route-variant coverage, not proven failures. Gemma and 20B both demonstrated end-to-end tool calls through both harnesses on the OpenAI wire; no model-level tool conclusion is drawn for 120B. A later Claude/native-wire diagnostic run never created a Sandbox: its Bazel test process was blocked in host filesystem I/O before setup and was interrupted. It is not an attempted matrix cell (invocation `6015f2e6-68e3-4bff-adc1-1de94bf50c2e`).

## Path evidence and limits

- The Claude turn opened successfully, then failed in the harness with `API Error: Connection refused — a firewall or proxy may be blocking it (ECONNREFUSED)`. There was no matching decision for its Sandbox in the sampled application egress log and no Ollama generation request at that time. Archived thread events show Claude's first two `api_retry` notices at 22:11:00.389 and 22:11:00.977 UTC, about two seconds after the sidecar container started at 22:10:58; the sidecar has no listener-readiness probe. The retry records expose no URL or socket. This makes local proxy startup a testable hypothesis, not a proven refused endpoint.
- Codex's Sandbox generated allowed application egress decisions for POSTs to `agentplane-llm-ingress:8080` at 22:12:51 and 22:13:33-34 UTC. Ollama logged a 200 response to `/v1/responses` at 22:13:33, the first call taking 41.7 seconds during model loading; later responses were under half a second. This proves one full model path worked. The application egress decision is not itself a Cilium kernel verdict.
- Live Cilium policies in `agentplane-testing` are Valid and select the relevant Pod labels. The runner policy allows TCP 8888 to `agentplane-egress`; the egress policy allows runner ingress on 8888 and TCP 8080 to `agentplane-llm-ingress`; the ingress policy allows egress ingress on 8080 and TCP 4000 to LiteLLM. Live services had endpoints at each hop, including Ollama `10.244.5.13:11434`. There were no namespaced CNPs in `litellm` or `ollama`; the clusterwide `sandbox-force-proxy-egress` policy selects `claude-sandbox`, not `agentplane-testing`. These observations make a missing allow rule on the nominal path unlikely; they do not exclude an unobserved Cilium drop or a separate direct path taken by Claude.
- Ollama logged a GPU discovery watchdog timeout at 22:10:45 UTC, but the first Claude failure was a connection refusal before a logged Ollama request. The later Codex call incurred a measured 41.7-second cold model load. Image pulling for the first Sandbox was a separate startup delay before any turn.
- The native Ollama wire accepted network traffic but rejected the `gpt-oss-20b` Codex request shape with HTTP 400. This is distinct from the Claude connection failure and the Codex OpenAI-wire tool refusal.
- The fresh Claude 20B Pod's sidecar container started at 22:55:40 UTC but only logged `relaying 127.0.0.1:3128` at 22:55:46, a six-second gap. The first failed Claude turn's retries were only about two seconds after its sidecar started. Combined with no sidecar readiness/startup probe in the [sandbox Pod spec](../../cluster/cdk8s/agentplane/sandbox_pod.py) and no matching application egress decision, this supports a listener-startup race; the exact refused socket on the failed turn remains unobserved. The fresh turn passed with recorded shell output after the listener came up.
- A fresh real Codex 20B turn also passed with native `commandExecution` and recorded shell output. This establishes that the first refusal is not a persistent route/tool incapability; its one-off cause remains unknown because the original upstream tool schema was not captured.
- At 22:17:36 UTC the 120B Codex request passed application egress. Ollama identified a 120B model and began loading tensors, with later `llm server loading model` entries. No load-complete or generation response was observed. Ollama's first request closed at 22:27:36 with HTTP 499 after exactly ten minutes; a second load returned HTTP 500 at 22:34:48 after 7m11s; a third request closed at 22:44:50 with HTTP 499 after ten minutes. LiteLLM's single observed 120B response to the Agentplane ingress IP `10.244.9.96` was HTTP 408 at 22:44:52, with `Timeout passed=600.0`; ingress logged receipt of that 408 at the same instant. This is evidence of repeated Ollama requests beneath one still-open ingress-to-LiteLLM request after the Sandbox was deleted. LiteLLM's exact retry decision/attempt count was not captured; no new Agentplane turn was observed. At 22:44:52 the proxy request had terminated, so there is no longer a scoped request to cancel. Do not cancel an unrelated shared request or unload a model.
- The shared `Client.events(read_seconds=300)` bound is per received SSE frame, not an absolute turn deadline. It continued past 300 seconds because the connection remained open. The new manual driver now wraps `Agent.run` in an absolute `asyncio.timeout(300)` for subsequent runs. The existing 120B process was interrupted at 410 seconds and was not retried.
- The Codex runner config sets `approval_policy=never` and `sandbox_mode=danger-full-access`; no completed tool item appeared in its OpenAI-wire turn. The final response claimed no shell permission. We did not capture the upstream tool schema, so these observations do not distinguish a model refusal from tools omitted or malformed in the LiteLLM/Ollama Responses request. Similarly, the Claude failure occurred before an application egress decision, but the exact refused socket was not captured.
- A bounded direct request to deployed LiteLLM using the authorized cheap-experiments key reproduced the native-wire error without loading a model: POST `/v1/responses` with `model=ollama/olm-chat/gpt-oss-20b-128k`, `reasoning={effort:low,summary:auto}`, and one output token returned the same Ollama HTTP 400 `think must be a boolean or string` in about two seconds. The key stayed in process memory and was not logged or saved. The deployed LiteLLM image is `tana-litellm-proxy:devel-20260921022830-13b3054`, whose [source pin](../../tana/litellm_proxy/requirements.in) specifies LiteLLM 1.100.1. In that version the [Responses bridge](https://github.com/BerriAI/litellm/blob/v1.100.1/litellm/responses/litellm_completion_transformation/transformation.py) retains the full `reasoning` object when `summary` is present, and the [Ollama chat transformation](https://github.com/BerriAI/litellm/blob/v1.100.1/litellm/llms/ollama/chat/transformation.py) copies the resulting value to `think` for `gpt-oss`. This source chain plus the matching live error identifies the malformed transformation; the exact Codex upstream request body was not captured.
- The 120B model is 65GB on the `llm-models` PVC, an ext4 OpenEBS local LVM volume in `openebs-proxmox-hdd` on wyrm2. Ollama's loader waited in kernel `folio_wait_bit_common`; `/proc/PID/io` `read_bytes` rose from 20,431,060,992 to 21,722,906,624 in successive samples, so it was reading rather than frozen. Its cgroup showed 13.7GB in use of a 40GB limit, zero memory-max/OOM events, and I/O pressure `full avg60=88.42%`. The 300-second agent wait and the upstream ten-minute request lifetime both ended before the cold load completed. Memory OOM was not observed. The deployed config has no explicit Ollama-route `timeout` or `num_retries`; the observed 600-second timeout and repeat attempts therefore need attribution to the pinned LiteLLM/runtime defaults, not the route declaration. `ollama show` advertises a 131072-token architectural context and tool capability, but that does not prove actual runtime context or end-to-end tool support.
- Direct Ollama 20B `/api/chat` with an explicit function schema emitted `emit_marker({value: "OLLAMA_PROBE_OK"})` over HTTP 200; after a tool-role result, a second HTTP 200 reported the marker with no further tool calls. This direct probe used `num_ctx=4096`, not the offered 128k alias. An equivalent authorized LiteLLM `/v1/responses` request on `ollama/oai-chat/gpt-oss-20b-128k` returned `status=completed`, output types `reasoning` and `function_call`, the same function name and marker arguments. Credentials stayed in process memory; only these structural results were retained. The model and LiteLLM can therefore carry tool calls when explicitly supplied, but the first Codex request's tool metadata remains unobserved.
- Gemma's loader began at 22:59:09 UTC and reported `llama-server started in 333.77 seconds` at 23:04:43, after the Claude turn's absolute 300-second limit expired. No model-level tool outcome was observed. The Sandbox was cleaned; the pre-header upstream request may outlive the client as the 120B request did.
- The first Gemma request eventually completed upstream with HTTP 200 at 23:05:08 UTC, after the client had timed out. A subsequent Claude repeat passed with recorded tool output despite another model reload caused by shared traffic; the Codex Gemma turn passed while the model remained warm. The cold timeout was therefore a load-budget failure, not a demonstrated Gemma tool incapability.
- The deployed `gemma4-31b-it-q8_0-128k` alias supplied no explicit context option, while Ollama `/api/ps` reported its live `context_length=262144` and the llama-server command used `-c 262144`. `/api/show` reported architectural `gemma4.context_length=262144` and no `num_ctx` model parameter. The deployment sets `OLLAMA_NUM_CTX=131072`, but [Ollama v0.34.0 envconfig](https://github.com/ollama/ollama/blob/v0.34.0/envconfig/config.go) reads `OLLAMA_CONTEXT_LENGTH` instead. Thus the 128k route suffix does not describe the observed Gemma runtime context. Exact v0.34.0 OpenAI-compatible [ChatCompletionRequest](https://github.com/ollama/ollama/blob/v0.34.0/openai/openai.go) and [ResponsesRequest](https://github.com/ollama/ollama/blob/v0.34.0/openai/responses.go) structs have no `options` field, and their conversion functions construct the native options map only from supported request fields; therefore passing `extra_body.options.num_ctx` through the OpenAI-compatible wire is not an established fix and appears ignored at that endpoint. Native `/api/chat` does accept `options.num_ctx`. No config fix was applied.
- The [ingress implementation](../llm_ingress/app.py) awaits `backend.send(..., stream=True)` before it creates `StreamingResponse`; its `finally: upstream.aclose()` runs only inside that response body. There is no disconnect watcher during the pre-header wait. This is a source-backed cancellation propagation gap consistent with the 120B request persisting after the Agentplane Sandbox was deleted. The deployed LiteLLM config has no route-level retry/timeout overrides; pinned LiteLLM 1.100.1 [Router](https://github.com/BerriAI/litellm/blob/v1.100.1/litellm/router.py) falls back to `openai.DEFAULT_MAX_RETRIES` if neither router nor global retries are set. The observed three upstream requests are consistent with that default, though their request IDs were not logged.

No service, policy, deployment, or model configuration was changed; normal inference did load and evict models. A separate change owns the sidecar readiness gate. Other follow-up: capture sanitized Codex request tool metadata if the intermittent refusal recurs; correct the native Ollama `think` value; and address 120B cold-load cost and pre-header cancellation before attempting its other harness or larger contexts. These are recommendations, not implemented fixes.

## Post-readiness rerun (2026-09-24 23:55–2026-09-25 00:00 UTC)

[PR #7902](https://github.com/agentydragon/ducktape/pull/7902) had merged before this rerun. The live `agentplane-testing/agentplane-runner` SandboxTemplate, generation 47, used egress-sidecar image `devel-20260924234050-4819c98` and an HTTP readiness probe for `/readyz` on port 3129 (2-second period, 1-second timeout). This was verified in the cluster, not inferred from merge status. On the fresh Claude test Pod `accept-ollama-smoke-fnw6f`, the runner started at 23:55:18 and the sidecar container at 23:55:25. The Pod remained `Ready=False` and the sidecar `ready=false` after container start. The sidecar logged the `127.0.0.1:3128` relay and `/readyz` listener at 23:55:39.648, kubelet recorded `/readyz` HTTP 200 at 23:55:41.540, and a subsequent read showed Pod and sidecar `Ready=True`. This directly verifies that the new image serves the endpoint and the Pod readiness gate waits for the listener.

Two bounded post-readiness 20B OpenAI-wire turns both passed with actual recorded shell-tool output:

| Harness | Bazel invocation                       | Thread                                 | Recorded tool output                                                | Result                    |
| ------- | -------------------------------------- | -------------------------------------- | ------------------------------------------------------------------- | ------------------------- |
| Claude  | `bcd6edf8-5491-4a0b-a754-8a7e2b426065` | `511caf73-e6e4-4e2d-a40d-b9499c24b9d8` | `/state/work` and `OLLAMA_SMOKE_TOOL_OK`                            | Completed, correct answer |
| Codex   | `a2818bbb-e749-418c-a9a5-c0c15fecd601` | `5fd7f5b0-4b97-4f53-b2a7-04fd3eff67d8` | `/state/work` and `OLLAMA_SMOKE_TOOL_OK`; native `commandExecution` | Completed, correct answer |

The first test took 228.5 seconds total including a 30-second diagnostic Pod hold. Ollama began a cold 20B load at 23:56:00 and logged `llama-server started in 123.71 seconds` at 23:58:04; its first `/v1/responses` request returned HTTP 200 at 23:58:05. Thus the long delay was model loading, not a sidecar or tool failure. After that turn, `/api/ps` reported `gpt-oss:20b` with runtime `context_length=131072` and `size_vram=13234832669`; this agrees with the 128k alias but does not prove the alias configured it, because the model itself advertises 131072. Both isolated test Sandboxes were cleaned up.

Gemma was deliberately deferred until the separate context-setting correction is deployed, to avoid another redundant cold load. At 00:00 UTC the live Ollama 0.34.0 Deployment still had `OLLAMA_NUM_CTX=131072`, not `OLLAMA_CONTEXT_LENGTH`; therefore this rerun validates sidecar readiness but **not** the proposed context fix. The prior Gemma passes remain valid observations, and the prior 120B limit remains unresolved; neither model was rerun here. `ducktape-flux/agentplane-testing` was Progressing/Ready Unknown at the final query despite the live generation-47 template and both successful turns, so this report does not claim the entire Flux Kustomization was Ready. The next bounded verification is Gemma through both harnesses plus `/api/ps` context once the corrected deployment is observed live.

Upgrade assessment: [Ollama 0.34.4](https://github.com/ollama/ollama/releases/tag/v0.34.4) is newer than deployed 0.34.0, but its [envconfig](https://github.com/ollama/ollama/blob/v0.34.4/envconfig/config.go) still uses `OLLAMA_CONTEXT_LENGTH`, and its OpenAI-compatible [chat](https://github.com/ollama/ollama/blob/v0.34.4/openai/openai.go) and [Responses](https://github.com/ollama/ollama/blob/v0.34.4/openai/responses.go) request structures still have no `options` field. It does not provide evidence of a fix for the LiteLLM `reasoning` object passed as `think` or Agentplane's pre-header cancellation behavior. No Ollama upgrade was performed.

## qwen3.8-flash-next-q4 direct-Ollama probing (2026-09-26)

Scope: `qwen3.8-flash-next-q4` (`metalspork/qwen3.8-flash-next-ud:UD-Q4_K_XL`, 112GB on disk),
added to the roster in #7971 but never live-tested. This section covers direct probing of
the Ollama native `/api/chat` endpoint (via `kubectl port-forward`), not yet the Agentplane
harness/Sandbox path in the section above.

Cluster Ollama itself was paused for host inference experiments between #7907 and #8000
(2026-09-24 to 2026-09-26); the model roster entry existed in git the whole time but the
Ollama Deployment was at `replicas: 0`, so the model was never pulled until #8000 re-enabled
it and the `setup-gpt-oss-v4` Job ran.

**Capability tag**: Ollama's own `/api/tags` reports this model's `capabilities` as
`["completion", "vision"]` — no `"tools"`, unlike `gpt-oss`/`gemma4:31b-it-q8_0` which both
report `"tools"`. `ollama show --template` confirms the embedded Jinja template does have a
tool-call branch (`{%- if tools and tools is iterable ... %}`), but it renders tool calls as
`<tool_call>\n<function=name>\n<parameter=x>\nvalue\n</parameter>\n</function>\n</tool_call>` —
an XML-tag format, not the JSON-object format (`{"name": ..., "arguments": {...}}`) most
tool-call templates and Ollama's own capability-detection heuristic expect. This is the
likely reason Ollama doesn't tag the model as tool-capable, even though the template clearly
intends to support tool calls.

**Confirmed working despite the missing tag**: a direct `/api/chat` request with a `tools`
array and `num_ctx=4096` (2026-09-26 07:55 UTC, once the load-timeout and contention fixes
below were live and the model was warm) returned a correctly structured response —
`message.tool_calls[0]` = `{"function": {"name": "get_weather", "arguments": {"location":
"Paris"}}}`, plus real content in a separate `message.thinking` field, both parsed cleanly by
Ollama out of the model's own XML-tagged template output. So the capability tag is simply
wrong/stale for this third-party model, not a functional block: Ollama parses this template's
tool-call and thinking output correctly, it just doesn't advertise that it can.

**Cold-load failures, root-caused**: every direct-probe attempt at a fresh Ollama pod (several,
between 2026-09-26 04:58 and 07:28 UTC) failed with the Ollama container logging
`"Load failed" ... error="timed out waiting for llama-server to start - "`, and **no further
load activity for that model followed** — contrary to this doc's earlier assumption (written
by an earlier pass over this same session) that a failed request's model keeps loading
server-side and a retry benefits from it. That assumption was wrong for this model: tracing
`llama_server.go:433`/`sched.go:641` log lines by blob hash across the session showed the
apparent "success shortly after a failure" pattern was two different models' blobs
interleaved (qwen failing, then a different, smaller model's request succeeding), not qwen
recovering. Root cause: `OLLAMA_LOAD_TIMEOUT` (default `5m`, confirmed via
`ollama serve --help`: "How long to allow model loads to stall before giving up") is shorter
than a cold read of this 112GB model off HDD-backed `lvm-proxmox-hdd` storage, and Ollama
abandons a stalled load once it elapses, with no automatic retry absent a new client request.

Contributing contention, since Ollama only keeps one large model resident at a time on
wyrm2's single GPU pair: `agentplane-index`'s `ducktape` worker continuously calls
`ollama.ollama.svc`'s `/v1/embeddings` as part of its normal operation (re-embedding on every
new commit to this repo — confirmed via its `/status` endpoint showing `pending_files: 0`,
`updating: false`, i.e. not backlogged, just keeping up with this repo's actual commit rate),
which repeatedly evicted the qwen load attempt before its own timeout could even be reached.

Both were fixed and merged:

- #8023: paused `agentplane-index`'s `ducktape` worker (`replicas: 0`) for the duration of
  this test; `haku-state`'s worker was left running; nothing else was found to depend on the
  `ducktape` index. Tombstoned for revert once this testing is done.
- #8030: raised `OLLAMA_LOAD_TIMEOUT` to `30m` on the ollama Deployment. A live-only
  `kubectl set env` test of this (before committing it) was silently reverted by Flux's
  drift correction after ~14 minutes, which is itself worth recording: **a live patch to a
  Flux-managed Deployment does not survive past the next reconcile**, and its rollback
  triggered a pod restart whose incidental model-load log line was initially (and
  incorrectly) mistaken for unrelated third-party traffic on the shared Ollama instance.

With both fixes live, a direct `/api/chat` probe (no `tools`, `num_ctx=4096`) completed a
cold load in `llama-server started in 708.47 seconds` (~11m48s), well inside the new 30m
budget. `ollama ps` afterward reported `87 GB`, `33%/67% CPU/GPU` split, confirming this
model does not fit in wyrm2's combined GPU VRAM and runs partly off CPU-mapped memory.
The container's own cgroup `memory.current` stayed at 24GB of its 40GB `memory.max` the whole
time — the CPU-resident portion is not being evicted by the memory limit.

**Generation throughput, separately from load time**: that same load-triggering "Say hi."
request (no tools, 4096 ctx) took a **total** of 20m6s end to end, logged by llama.cpp's own
`print_timing`: `prompt eval ... 0.18 tokens per second`, `eval time ... 0.14 tokens per
second`, for a 55-token prompt and a 27-token reply. This is not a one-off cold-start cost —
it is steady-state per-token throughput on this node, roughly three orders of magnitude
slower than an interactively-usable rate. The Ollama server serializes requests to this model
(`llama-server ... -np 1`, one parallel slot); a second, immediately-following tool-call probe
queued behind this one and was killed by its own 90s client timeout without ever starting.

At this throughput, any answer (including a correct tool call) is real but arrives on the
order of many minutes per turn, which functions as a de facto timeout failure in every
harness/proxy path this doc's other sections describe (Agentplane's 300s absolute turn budget,
LiteLLM/Ollama's default request timeout, a Codex/Claude Code CLI session). A follow-up
tool-call request on the same warm instance (2026-09-26 07:55 UTC, 319-token prompt, 72-token
reply including `thinking`) completed faster — `total_duration` 263.5s, ~1.44 tok/s generation
— but that is still roughly **20-30x slower** than the operator's own reference point (below)
and not a usable interactive rate by any reasonable bar; it does not change the conclusion.

**Root cause, better supported than the HDD/page-cache theory above**: the operator's working
comparison point is `ghcr.io/ggml-org/llama.cpp@sha256:014f721265...` (confirmed live:
`llama-server --version` → `0.5.0-dev, build 11151, commit bd4f514db`, authored
**2026-09-23**), run directly via `docker run` with `--fit-target 1024,1024` (this is llama.cpp's
own _default_ value, confirmed via `llama-server --help`, so not itself a difference) against
the same model file on a different, SSD-backed path, and got ~30 tokens/sec. Ollama 0.34.0
vendors its own llama.cpp at commit `0f3a71be1`, authored **2026-09-02** (confirmed live via
`llama-server --version` inside the ollama container) — a 21-day gap. Qwen3.8-Flash-Next
(`qwen4exp`) is a hybrid Gated DeltaNet (recurrent/SSM) + MoE architecture ("Qwen3 Next"
family); upstream llama.cpp history between those two commits includes several fixes and
optimizations specific to that hybrid layer type merged **after** ollama's pin: `models : fix
GDN normalization from max to rsqrt` (#28068, 2026-09-06), `convert : write explicit
recurrent_layers for Qwen3-Next / Qwen3.5` (#28208, 2026-09-07), plus later MoE/kernel work
(radix-select top-k #28670, raised expert limit for `mul_mat_id` #28501, contiguous-tensor
CUDA conversion #29155). This lines up with something already captured, unremarked, in this
session's own probe logs: `forcing full prompt re-processing due to lack of cache data
(likely due to SWA or hybrid/recurrent memory ...)` — direct, session-local evidence that
Ollama's older build is hitting a hybrid-architecture context-caching limitation for this
specific model, not merely a storage-speed effect. HDD-vs-SSD storage and GPU-VRAM packing
(the 33%/67% CPU/GPU split) may still be contributing factors, but the version gap plus this
specific, already-observed cache-invalidation message is the best-supported explanation and
was not something a Deployment-level config change could fix — it needs a newer llama.cpp
backend serving this model, not a different `OLLAMA_LOAD_TIMEOUT`/`memory` value. No such
change was made in this session; this is a finding to act on separately, not a landed fix.

**Summary**: the tool-call protocol question is answered — this model, served by Ollama
0.34.0, produces correctly structured `tool_calls` and a separate `thinking` field despite its
missing `"tools"` capability tag. The practical-usability question is not answered
favorably: generation throughput on this deployment (~0.14-1.44 tok/s, load-dependent) is
20-30x slower than the operator's own SSD-hosted, newer-llama.cpp reference (~30 tok/s), most
likely because Ollama 0.34.0's vendored llama.cpp (2026-09-02) predates hybrid
Gated-DeltaNet/MoE-specific fixes and optimizations this model's architecture needs, upstream
by 2026-09-23. Landed in this session: #8023 (temporary), #8030 (durable) — neither addresses
the throughput gap. Not attempted: bumping the Ollama image to a release with a newer vendored
llama.cpp, or standing up this specific model behind a directly-deployed, more-current
llama.cpp server (mirroring the operator's own working setup) instead of through Ollama.
