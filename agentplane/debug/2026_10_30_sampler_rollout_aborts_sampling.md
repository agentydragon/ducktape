# Sampler rollout abandons an active provider response

Read-only staging investigation on 2026-10-30 PDT (2026-10-31 UTC).
[Thread](https://agentplane-staging.allegedly.works/#/threads/afdf8240-f095-4742-87ff-877dab067fea):
`afdf8240-f095-4742-87ff-877dab067fea`; conversation
`1fc8b92b-0731-4580-85c9-4c916b871505`; sampling intent
`faa8637e-73e6-457e-baff-cd7bfe2c35ce`.

## Evidence

- At `01:50:00Z`, sampler event 91607 dispatched the generation-4 sampling intent to
  `ollama/oai-chat/qwen3.8-flash-next-iq4xs-256k`. Event 91609 records HTTP 200 at
  `01:50:03Z`.
- LLM ingress request `U4N6LPBKAHHG6OCQA2IMLGNDPN` retained 99 contiguous response chunks,
  numbered 0–98. Its stream contains `response.completed` and ends with `complete: true`
  at `01:50:08.523736479Z`. The response proposes one `exec` call,
  `Yzw8zK8rtZovwdy8iffjhJ0o6gISzzcE`, to inspect `/tmp/lc/`; it was never dispatched by
  the sampler.
- The sampler durably captured only the first 77,339 bytes of the ingress's 115,561-byte
  response stream. The bytes match exactly; both prefixes have SHA-256
  `114799b2b5971ad327bad9cb0ddcc294b172eba995aa5c9bc0d8daeb84d1e506`.
  Its decoded provider evidence stops at a reasoning-summary delta (event 91868), with
  no terminal response or sampling result. Raw received bytes extend slightly beyond
  the last decoded delta but still contain no completion or function call.
- Kubernetes recorded `Killing` for old Pod `central-sampler-64d7ffb796-skv69` at
  `01:50:12Z`, while scaling its ReplicaSet down. Its image was
  `devel-20261031004321-bfd2d24`.
- Replacement Pod `central-sampler-5589489f7d-wxrl4`, image
  `devel-20261031014747-aca2575`, started its serving container at `01:50:23Z`.
  At `01:50:23.33077391Z` it logged `earlier owner left an unresolved effect`.
  Events 91871–91872 block the Session and fail the turn. The conversation has generation 5,
  no owner, and the generation-4 sampling intent still pending with a null result.
- All 54 cells are completed, all 342 executor intents and 106 nested tool intents have
  results, and all 108 process rows are exited. Of 58 sampling intents, only this one is
  unresolved. There is no unresolved executor effect to replay or kill.
- The existing Sandbox Pod UID still matches the retained incarnation:
  `ce840e3e-5e45-447a-9f1a-63db2bcc519f`, `public-coder-ducktape-1`.
  Its workspace `/state/workspaces/afdf8240-f095-4742-87ff-877dab067fea` exists and
  `git status --short` is empty. The earlier `/tmp/lc/ghlinks.py` file also exists.

## Mechanism

The old image's `cmd/main.go` cancels the service context on SIGTERM. `Runtime.Run`
uses that context for active drivers, so shutdown cancels their provider ingestion rather
than waiting for a durable continuation boundary. `Runtime.drive` suppresses normal failure
settlement during service shutdown, then releases ownership. The next owner's
`Store.CheckRecovery` refuses any unresolved intent from an earlier generation.

The retained stream, rollout timestamps and source establish a rollout interruption during
response ingestion. The old Pod has no retained per-session cancellation log, so the precise
last canceled read or transaction cannot be identified. The provider completion is known
from ingress evidence; the sampler never committed it to its native conversation.

## Recovery and related work

No Session state, remote process, workspace or provider request was changed or replayed.
The current [recovery API](../x/central_sampler/docs/blocked_session_recovery.md) explicitly
refuses pending sampling and compaction; clearing flags in SQL is not a supported recovery.
A new Session can continue from the preserved workspace with an explicit handoff. Continuing
this exact Session needs a reviewed sampling reconciliation/abandonment operation; the completed
ingress response provides evidence for such a design without resampling the request.

[PR #12989](https://github.com/agentydragon/ducktape/pull/12989) merged cell/runtime recovery,
but excludes sampling. Open [#12933](https://github.com/agentydragon/ducktape/pull/12933)
exposes blocked state in the UI; open [#13033](https://github.com/agentydragon/ducktape/pull/13033)
and [#13182](https://github.com/agentydragon/ducktape/pull/13182) handle executor rejections.
Merged [#13180](https://github.com/agentydragon/ducktape/pull/13180) handles proven native
executor-session loss. None covers this sampling boundary. The rollout's
[#13138](https://github.com/agentydragon/ducktape/pull/13138) drains inbound gRPC; active sampler
drivers have a separate lifetime and remain canceled on shutdown.

Track the ownership and rollout problem as
[`CENTRAL_SAMPLER_REPLICA_ROLLOUT`](../plans/task_dag.md#central_sampler_replica_rollout--multiple-replicas-and-rolls-that-preserve-active-agents).
Scratch captures and the offline protobuf decoder are under `/tmp/sampling-faa8637e/` and
are ephemeral. The ingress stream can be located in Loki by the request ID above, using
`namespace="agentplane-staging", app="llm-ingress"` and `01:49:55Z`–`01:50:30Z`.
