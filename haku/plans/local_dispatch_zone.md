# Haku local dispatch zone plan

Goal: add a Haku worker zone that dispatches to local Ollama-hosted models through the
existing two-layer LiteLLM dispatch plane, with a scheduler that prevents model-swap
thrash by allowing at most one active local model at a time.

The scheduler should be designed as an optional general feature, not a local-only
special case. Most targets can keep today's behavior: a model allowlist with no
parallelism throttle. Local inference needs grouping for model residency, and some
hosted-provider model lanes may later want it for shared quota windows, accounts,
rate-limit buckets, context caches, or other operational constraints.

This plan should also stop treating "zone" as one overloaded concept. The durable model
should separate:

- **Model lane**: provider/key path/model allowlist/scheduling facts.
- **Isolation profile**: namespace, egress, mounts, MCP servers, and other runtime
  capabilities.
- **Dispatch target**: an explicitly allowed pairing of one model lane and one isolation
  profile.

That split matters because the operator cares about invariants like "z.ai models should
never get access to Google Drive." A classifier policy is not enough for that. The
dispatcher should make the invalid pairing unrepresentable: no dispatch target combines
an external-low model lane with a Drive/Gmail/Tana-capable isolation profile.

The initial local target is analogous to the existing `zai` target, but with a different
trust and scheduling profile:

- Prompts stay on local infrastructure, so the admission policy does not need the
  z.ai/public-by-construction restriction.
- Workers are still low-trust. They must not get haku-state, broad cluster access, raw
  provider keys, or an unrestricted LLM key.
- Local inference is capacity-bound by model residency. Dispatch must avoid interleaving
  jobs across different large models.

## Config shape

The eventual config should look more like this than today's flat `zones.yaml`:

```yaml
model_lanes:
  zai:
    provider: z.ai
    trust_tier: external-low
    upstream_key: haku-lane-zai
    models:
      glm-4.5-air-anthropic: {}

  local-20b:
    provider: local-ollama
    trust_tier: local
    upstream_key: haku-lane-local
    scheduling:
      max_active_model_groups: 1
      max_concurrent_jobs_per_model_group: 1
    models:
      gpt-oss-20b-128k-openai-chat:
        model_group: gpt-oss-20b

isolation_profiles:
  public-only:
    namespace: haku-sandbox-zai
    capability_tier: public-only
    mcp_servers: []
    egress_profile: public-web

  local-default:
    namespace: haku-sandbox-local
    capability_tier: local-default
    mcp_servers: []
    egress_profile: locked-down

  google-drive-readonly:
    namespace: haku-sandbox-drive-ro
    capability_tier: personal-data-readonly
    mcp_servers:
      - google-drive-ro
    egress_profile: locked-down

dispatch_targets:
  zai-public:
    model_lane: zai
    isolation_profile: public-only

  local-default:
    model_lane: local-20b
    isolation_profile: local-default

  local-drive-ro:
    model_lane: local-20b
    isolation_profile: google-drive-readonly
```

There should be no target like `zai-drive-ro`. The dispatcher validates only configured
targets, and tests enforce provider/capability compatibility. For v1 implementation, this
can be a migration from today's `zones.yaml`; do not add another flat "zone means
everything" concept.

Required invariants:

- `external-low` model lanes, including z.ai, may pair only with `public-only`
  isolation profiles.
- Profiles exposing Google Drive, Gmail, Tana, Plaid, or other personal-data MCP servers
  must not pair with external-low model lanes.
- Every dispatch target names exactly one model lane and one isolation profile.
- The per-job LiteLLM key is scoped to the selected model from the model lane.
- The worker namespace/profile, not the model lane, determines MCP server access,
  mounted secrets, and network policy.

## Initial model allowlist

Only whitelist models that pass live smoke probes for the API shape the worker harness
uses. As of the LiteLLM probe run in July 2026, the useful local route is the
OpenAI-compatible Ollama route, not the `ollama-native` route.

Seed the local zone conservatively:

```text
gpt-oss-20b-128k-openai-chat
```

Candidate additions after another explicit smoke run:

```text
gpt-oss-20b-256k-openai-chat
gpt-oss-20b-512k-openai-chat
gpt-oss-20b-1m-openai-chat
gpt-oss-120b-128k-openai-chat
gemma4-31b-it-q8_0-128k-openai-chat
```

The 20B `*-openai-chat` routes passed text and tool calls across OpenAI Chat,
OpenAI Responses, and Anthropic Messages shapes. The 120B and Gemma routes also passed
Anthropic-shaped text/tool probes after warmup, but they showed cold-load instability
elsewhere, including 500/504 responses. Do not make them the default local worker model
until the operator accepts that operational profile.

Do not include `*-ollama-native` gpt-oss routes in the worker allowlist for now. They
handled basic text but failed structured tool calls and Responses-shape validation.

## Smoke-test gate

The model allowlist should be an output of a repeatable probe, not a hand-maintained
guess. The probe contract for a local worker model should include at least:

- Anthropic Messages text: final text exactly `OK`.
- Anthropic Messages tool call: a `tool_use` block named `lookup_demo_fact` with input
  exactly `{"topic": "litellm-probe"}`.
- Optional parity probes for OpenAI Chat and OpenAI Responses, kept for diagnostics even
  if the worker harness uses Anthropic shape.

Use the existing `//cluster/k8s/litellm/app:probe_models` target for this. Its
`results.jsonl` records are keyed by `POST + URL + canonical JSON request body`, so
reruns can preserve successful checks and rerun only failed or missing exchanges.

Before adding or keeping a model in the local model lane, attach the probe report path in
the PR description and update this plan if the expected model set changes.

## Dispatch scheduling

The local zone needs a model-residency scheduler in front of Kubernetes Job creation.
Kubernetes cannot express "at most one distinct model label is active" by itself.

Add optional model-lane scheduling config, for example:

```yaml
model_lanes:
  local-20b:
    provider: local-ollama
    trust_tier: local
    scheduling:
      max_active_model_groups: 1
      max_concurrent_jobs_per_model_group: 1
    models:
      gpt-oss-20b-128k-openai-chat:
        model_group: gpt-oss-20b
```

Semantics:

- If a model lane has no `max_active_model_groups`/`model_group` config, there is no
  active-model-group scheduling limit; the dispatcher only enforces the model allowlist
  and per-job key budget/TTL.
- If no local jobs are active, the dispatcher may start the next queued local job and
  that job's `model_group` becomes the active local model group.
- If local jobs are active and the next job uses the same `model_group`, the dispatcher
  may start it only if
  `active_jobs_for_model_group < max_concurrent_jobs_per_model_group`.
- If local jobs are active for a different `model_group`, the dispatcher must keep the
  job queued until the active group drains.
- Failed, succeeded, killed, and timed-out jobs release their active slot.

Start with `max_concurrent_jobs_per_model_group: 1`. Raising it later is a local capacity
decision, not an isolation-profile change.

For remote-provider model lanes, the same optional schema can express different grouping
without preventing multiple model strings. Examples:

```yaml
model_lanes:
  zai:
    scheduling:
      max_active_model_groups: 2
    models:
      glm-4.5-air-anthropic:
        model_group: zai-low-cost
      glm-5.2-anthropic:
        model_group: zai-high-capability

  anthropic:
    scheduling:
      max_active_model_groups: 1
    models:
      claude-haiku-4-5:
        model_group: haku-cloud-workspace
      claude-sonnet-5:
        model_group: haku-cloud-workspace
```

The grouping key is an operator-owned scheduling fact, not necessarily the literal model
name. Local groups represent loaded model residency; hosted-provider groups can represent
shared account quota, cost lane, rate-limit bucket, or a desired "do not mix while a
batch is running" policy. Omit it where parallelism does not matter.

## Dispatcher changes

The current dispatcher stamps Jobs during `POST /jobs`. For local scheduling, split
admission from execution:

1. Accept and persist the job after auth, credential lint, classifier admission, dispatch
   target validation, model allowlist check, model-group resolution, and per-job budget
   validation.
2. Mark jobs that cannot run immediately as `queued` rather than creating a Kubernetes
   Job.
3. Add a dispatcher-owned scheduler loop or reconciliation endpoint that claims runnable
   jobs and stamps their Kubernetes Job + Secret.
4. Keep idempotency semantics: retrying the same idempotency key returns the existing DB
   row, whether queued or running.
5. Keep per-job LiteLLM keys minted close to execution time, not indefinitely at queue
   time, so TTL and budget windows reflect actual runtime.

The active-model-group calculation should come from dispatcher DB state, not from pod
inspection. Pod state is useful for repair, but the dispatcher owns the scheduling
contract.

## Perimeter and key chain

Reuse the existing three-hop key chain:

- Main LiteLLM serves the local model names.
- A static `haku-lane-local` LiteLLM virtual key on the main LiteLLM is allowlisted only
  to the local model lane's models and reflected into `haku-dispatch`.
- workers-LiteLLM exposes `litellm_proxy/<local-model>` entries using that static
  upstream key.
- The dispatcher mints per-job virtual keys on workers-LiteLLM, scoped to the requested
  local model, budget, and TTL.
- Worker pods receive only the per-job key and result token.

Add or update:

- `tf/gitops/litellm-keys/main.tf`: `local_lane_models`,
  `litellm_key.haku_lane_local`, reflected `litellm-key-haku-lane-local`.
- `cluster/k8s/haku/dispatch/litellm/generate_workers_litellm.py`: local model entries
  chained through `litellm_proxy/`.
- `cluster/k8s/haku/dispatch/dispatcher/zones.yaml`: migrate to model lanes,
  isolation profiles, and dispatch targets; add a local target.
- `haku/dispatch/test_zones_config.py`: expected target set, model-lane parity, and
  provider/capability compatibility invariants.

## Namespace perimeter

Create `haku-sandbox-local` as a peer to `haku-sandbox-zai`:

- No haku-state mount or source credential.
- Dispatcher can create only same-named Jobs and per-job Secrets in the namespace.
- workers-LiteLLM CNP admits local-zone pods.
- Local-zone pod egress should be no broader than needed for the worker harness. If local
  jobs need public git clone access, allow that intentionally; do not inherit broad
  `haku-sandbox` egress by accident.
- Keep the same result submission path: worker posts output to the dispatcher with the
  job-scoped result token.

The local model provider is in-cluster, but the worker still must be treated as
untrusted. A compromised worker should get at most its namespace, its per-job key, and
its result token.

## Classifier policy

Add a classifier policy selected by dispatch target/model lane, not only by namespace.
The `local` policy is separate from `zai`.

Suggested initial stance:

- Allow private/local work that would be inappropriate for z.ai, because the model stays
  in the cluster.
- Still reject credentials, API keys, PEM/JWT material, and instructions that ask the
  worker to exfiltrate or persist secrets.
- Still reject prompts that would require haku-state access unless the task explicitly
  provides a bounded public repo or artifact to fetch.
- Prefer narrow, task-shaped prompts. Local does not mean "full Haku privileges."
- Reject any request whose target/profile pairing is not configured, before the
  classifier runs.

The deterministic credential lint remains provider-independent and still runs before the
classifier.

## Open decisions

- Whether the local worker harness should use Claude Code CLI over Anthropic shape or a
  smaller custom harness. Anthropic shape is already smoke-tested and preserves tool-use
  structure on the recommended local routes.
- Whether to migrate the public API from `zone` to `target` immediately, or accept
  `zone` as a compatibility alias for a dispatch target during the transition.
- Whether 120B belongs in the initial zone or remains an operator-triggered model only.
- Whether to expose readable reasoning/thinking in worker result artifacts, redact it,
  or keep it only in Langfuse. Local gpt-oss and Gemma routes have emitted readable
  reasoning fields in probe responses.
- Whether the scheduler should have a manual "pin active model" override for long
  batches.

## Build order

1. Land the model-lane/isolation-profile/dispatch-target schema and tests with no new
   live target.
2. Add the local model list to the main LiteLLM key Terraform and workers-LiteLLM
   generator, parity-tested.
3. Add the `local-default` isolation profile namespace perimeter and workers-LiteLLM CNP
   admission.
4. Add a `local-default` dispatch target with only `gpt-oss-20b-128k-openai-chat`.
5. Run live smoke through workers-LiteLLM using a per-job-style key.
6. Dispatch one low-risk local job, verify result submission, budget accounting, and
   active-model release.
7. Only then expand the allowlist.
