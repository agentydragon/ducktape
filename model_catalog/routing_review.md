# LLM routing contract review

Status: proposed investigation, opened 2026-10-10 under [#9574](https://github.com/agentydragon/ducktape/issues/9574).
The operator requested recording the findings and reconsidering the routing design,
including alternative gateways. **No replacement, catalogue suppression, budget change,
route reactivation or runtime migration is approved by this document.** Existing
configuration stays in place while its behavioral contract is evaluated.

## Work ordering: routing decision before further roster implementation

Operator direction, 2026-10-10: prioritize this review **before further roster
restructuring**, not only before token-metadata expansion. The chosen routing design
may eliminate LiteLLM configuration and projection responsibilities; do not simplify
or generalize wiring we may no longer need. Keep existing behavior and record necessary
maintenance separately rather than treating the remaining roster inventory as an
implementation queue during this review.

Sequence: agree the client/protocol/context acceptance matrix, map current behavior,
evaluate where routing/translation/policy should live and compare options, then record
a routing decision. Only then revise the roster design and resume implementation with
the smaller set of responsibilities actually required. This is an investigation-first
checkpoint, not approval to replace LiteLLM or build a new abstraction layer.

## Why reopen the design

The [LiteLLM audit](litellm_metadata.md#catalogue-fallback-and-request-defaults-2026-10-10)
found that token metadata can become request defaults, not just discovery output.
Our clients do not derive their context budgets from LiteLLM's token-limit publication.
Removing our overrides restores catalogue/adapter fallback; it does not disable it.
The catalogue also carries pricing and capabilities. A switch to its bundled version
is not catalogue disablement, and clearing the whole map risks unrelated behavior.

Do not treat completing every token declaration as a prerequisite for this review.
The previous all-routes publication target is under reconsideration, not silently
implemented, deleted, or replaced. Preserve existing provenance and unresolved
acceptance work; this is not a reason to run another capacity-probe campaign.

## Primary success criterion: working clients and usable route context

Operator clarification, 2026-10-10: clients must work for the actual tasks and protocols
we need (including Anthropic Messages and OpenAI Responses), and should use the context
available on their selected route rather than an unnecessarily small fallback window.
A particular router, removing its catalogue, or publishing complete metadata is a means,
not the objective. Fixing only LiteLLM cannot solve a harness's independent fallback.

For every required client/route/protocol combination, record the complete chain:

| Layer                    | Evidence needed                                                                                                                   |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------- |
| Backend/account route    | Declared limits, measured accepted-input lower bounds, output/joint constraints, account/beta conditions and unknowns             |
| Gateway(s)               | Which requests are admitted, transformed, capped or rejected; whether explicit client settings survive every hop                  |
| Client model recognition | Which catalogue entry or fallback it selects for our exposed slug; supported way to supply route configuration or translate names |
| Client working budget    | Effective configured/usable window, reserves, compaction threshold and any clamp applied to an override                           |
| End-to-end result        | Real task/tool/stream behavior and evidence that no accidental lower fallback prevents use of the intended context                |

Do not equate input capacity with an input-plus-output budget, or require disabling
intentional compaction/output reserves to claim full context use. Conversely, a generic
272K client fallback on a route accepting over 900K is a mismatch to investigate, not
something fixed by publishing a larger number on an endpoint the client never reads.
Luna's 900419 accepted-input observation is a lower bound, not an exact maximum or
proof of useful coding performance throughout that window. Start with documented or
supported client configuration; evaluate alias/catalogue integration if overrides are
clamped or ignored. Name translation must preserve routing, authorization and responses.

The acceptance matrix must name active clients and their required features, including
tool calls, streaming, reasoning and model switches where used. Mark parked clients as
parked rather than activating them to fill the matrix. A longer-context working-session
check is a later, explicitly authorized acceptance step, not permission for more probes.

## First deliverable: current transformation map

Trace representative paths in both directions, with pinned implementation references:

```text
Harness / other client
  -> Agentplane ingress (where applicable)
  -> LiteLLM authentication, routing, adapters and callbacks
  -> CLIProxyAPI / direct provider / Ollama
  -> provider response and stream
  -> reverse adapters, accounting and client-visible events
```

The public route's shape describes the outbound wire, not the inbound API. Inventory
which inbound/outbound combinations are actually used before deciding to support or
remove any translator. Distinguish direct-provider accounts from subscription gateways
and parked consumers from active requirements.

For each hop, record **actual behavior / desired behavior / evidence / open question**:

- Authentication, virtual-key allowlists, tenant/account selection and credential ownership.
- Slug translation and hidden aliases, including response model names and harness recognition.
- Request parameter insertion, renaming, dropping and clamping: output limits, reasoning,
  temperature/sampling, system/developer roles, tools, structured output and multimodal data.
- Context prechecks, token estimation, truncation, compaction and model discovery. Keep
  client budgets, backend capacity, published claims and request caps separate.
- Native Responses and Messages semantics: tool IDs, reasoning/signature blocks, cached
  content, continuation IDs, beta headers, store behavior and unsupported parameters.
- Streaming event translation, usage, finish reasons, errors after HTTP 200, disconnect
  cancellation and backpressure. A successful HTTP status is not successful generation.
- Retry/fallback ownership across layers, especially after partial output; account and
  model substitutions must be explicit, and retries must not silently multiply paid work.
- Cost attribution, cache/reasoning usage, budget enforcement, metrics and audit logs.
  Separate provider prices from subscription-account accounting; preserve redaction.
- Embedding identity/dimensions and truncation; audio/image routes need modality-specific
  contracts rather than invented generative token windows.

Begin with Luna Responses, a Claude Messages route, chat-completions-to-Messages,
native versus OpenAI-compatible Ollama, and one embedding route. These exercise the
known risks without claiming to validate every combination or requiring paid inference.

## Proposed requirements to decide, not assumed features

1. Satisfy the client/protocol/context acceptance matrix above. Preserve native protocols when translation is unnecessary. Specify which cross-protocol
   translations we need and what information loss, if any, is acceptable.
2. Preserve explicit client parameters, or reject unsupported semantics clearly. If an
   upstream requires an omitted output limit, choose an explicit route policy rather than
   silently guessing from a model name. Account for downstream gateways stripping caps.
3. Unknown capacity stays unknown. Discovery must distinguish declared, measured and
   policy values; a descriptive field must not secretly become an enforcement rule.
4. Enumerate any allowed catalogue uses separately: pricing, capability hints, token
   defaults, admission checks and discovery. Decide which must be disableable or pinned.
5. Preserve credential boundaries, allowlists and spending controls independently of
   model recognition. No "unknown price means free" failure mode.
6. Make transformations and failures observable without logging credentials or raw
   sensitive prompts. Preserve upstream error details safely enough to diagnose them.
7. Bound operational complexity: prefer configuration or a small maintained integration
   over a router fork or a bespoke framework with implicit parity obligations.

## Options to compare after requirements

These are an initial research shortlist, **not audited recommendations or claims of
feature parity**. Pin versions and examine source/configuration, licensing, maintenance,
security boundaries and deployment cost before scoring them.

| Option                                                                                   | Question to answer                                                                                                                                |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| Retain LiteLLM with explicit policy / a targeted upstream change                         | Can token behavior be controlled independently of pricing and capability discovery without a permanent fork?                                      |
| Native-protocol forwarding for selected routes; keep LiteLLM where translation is needed | Can fewer adapters eliminate transformations while preserving authorization, accounting and fallbacks? What replaces each removed responsibility? |
| [Bifrost](https://github.com/maximhq/bifrost)                                            | How do native Messages/Responses, parameter preservation, custom routes and catalogue dependencies behave?                                        |
| [Portkey Gateway](https://github.com/Portkey-AI/gateway)                                 | Which routing, translation, auth and accounting capabilities are available in the self-hosted edition, and what defaults are implicit?            |
| [TensorZero](https://github.com/tensorzero/tensorzero)                                   | Does its API/configuration model fit existing harness protocols without requiring another lossy translation layer?                                |
| [Envoy AI Gateway](https://github.com/envoyproxy/ai-gateway)                             | What native-protocol and policy coverage exists, and how much control-plane/integration work would our requirements need?                         |

A hybrid or retaining the current gateway may win. Do not select by provider count,
benchmarks, or marketing claims alone. "No suitable token-only switch found in the
inspected LiteLLM paths" is supported; "LiteLLM's catalogue is impossible to disable"
is not yet established.

## Evidence and acceptance before a decision

- First use offline/mock upstream contract tests through the repository's Bazel test
  harness. Capture forwarded requests and resulting client-visible responses/events.
- Cover explicit versus omitted output caps, known versus unknown slugs, catalogue
  present/absent, both wires, long-input admission, natural versus budget termination,
  errors inside SSE, cancellation, and retry/fallback after partial output.
- Verify authorization and accounting failures, including unknown prices and account
  substitutions. Compare existing behavior against requirements, not just gateway parity.
- Use narrowly scoped live smoke tests only where fixtures cannot answer the question;
  any additional paid/long-context requests need separate authorization. The two approved
  Luna probes are complete, not an ongoing probe allowance.
- Produce a decision record with supported routes/features, known losses, operational
  cost, migration/rollback plan and explicit operator approval before changing traffic.
- Keep the existing runner continuity, fresh-session/model-switch, workstation activation,
  and paused-storage acceptance obligations visible. A gateway review does not close them.
