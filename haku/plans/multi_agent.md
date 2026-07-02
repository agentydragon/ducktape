# Haku multi-agent architecture — plan, options, considerations

Status: **design agreed 2026-07-02**; build order steps 1–2 **landed** (LiteLLM
virtual-key DB + TF-managed zone keys, PR #2739; `chatgpt` provider on devel) — see
_Build order_ for per-step state. This
plan splits Haku from one agent grinding all responsibilities into a high-intelligence
orchestrator plus cheaper, lower-trust worker zones. It complements
[runtime_options.md](runtime_options.md) (the orchestrator runtime stays deliberately
unpinned — see _Decisions_) and extends `haku/PLAN.md`'s durable doctrine to a fleet.

## Goal

- **Haku, the orchestrator** (Anthropic, full current perimeter) keeps all judgment:
  synthesis, item authoring, operator-model updates, anything reading
  Gmail/Drive/Plaid/Tana content. Its in-session subagents (Task tool) share its trust —
  parallelism, not a separate trust level.
- **Cheap worker zones** take well-scoped jobs the orchestrator dispatches: a **z.ai/GLM
  zone** (cheap, untrusted with personal data) and an **OpenAI zone** (trust between
  z.ai and Anthropic; subscription-billed). Workers hold a strict subset of Haku's
  privileges and capped budgets, so Haku may dispatch autonomously.
- **Sensor agents**: prompt-less, one-way watchers (website/price/release changes) that
  only send findings _to_ Haku; nothing flows out to them.
- The operator's Anthropic metered overage (~$2–3k/mo) shrinks by routing grunt work to
  ~$0.4/M-token GLM and flat-rate OpenAI subscription inference.

## Decisions already made (operator, 2026-07-02)

- **Namespace naming**: `haku-sandbox-zai` (and by extension `haku-sandbox-oai`).
- **No raw z.ai key anywhere near Haku or workers** — only LiteLLM holds provider keys;
  workers get scoped LiteLLM virtual keys. This prevents Haku accidentally sending
  personal data to z.ai: Haku never holds a GLM-capable credential at all.
- **The model boundary is per-context, fixed at spawn.** Everything in an agent's
  context window goes to its model provider, so model choice is an attribute of the
  _agent_, matched to the sensitivity of everything that agent will ever read. Never
  route one "cheap step" of a sensitive agent's loop to a cheaper provider (e.g. a
  summarize-model knob pointed at GLM would ship the whole context).
- **Classification gate before execution**, using an Anthropic-based classifier ("does
  this prompt contain PII / operator-personal data?"). The gate is a **thin dispatcher
  bolted in front of an existing job→execute plane** — it is not a reason to build a
  bespoke dispatch framework.
- **Enforcement lives in ducktape, judgment lives in haku-state.** Automated checks
  (PII classifier, gate wiring), worker images, and Job templates are ducktape-reviewed
  code — Haku can call but not modify them, so it structurally cannot bypass its own
  gate. Haku's routing policy and per-model calibration live in its state.
- **Haku gets PR authorship against ducktape on Forgejo** (not GitHub) — its third
  bounded write surface (after `haku-state` and `haku/` Gmail labels). Merge stays with
  the operator; the PII/CI checks gate the PRs.
- **Git is not a required substrate** for dispatch or audit — Langfuse traces +
  Kubernetes objects suffice; a job table earns its place later if needed.
- **Haku and its in-session subagents are one trust level** (no separate subagent
  class).
- **Runtime stays unpinned**: Haku keeps running as the Claude Code web routine for now;
  the dispatch interface is runtime-agnostic on both sides, so Haku can later move to
  Managed Agents (B) or the self-hosted loop (C) without touching the worker zones.
- **Local-GPU zone deferred** (operator's GPU is not always reachable) — noted as a
  future zero-egress zone for sensitive-but-mechanical bulk work (e.g. mail triage).

## Trust model

The provider a model runs on is **part of the egress surface**: whatever enters a
worker's context is disclosed to that provider. Combined with `haku/PLAN.md`'s "the
container is the trust boundary," each zone is bounded by two mechanisms, neither of
which is agent restraint:

1. **Namespace perimeter** — what the worker can _reach_ (secrets not reflected, egress
   FQDNs not allowlisted). Even a fully prompt-injected worker cannot fetch Gmail/Drive/
   Plaid, because the credential is not there and the egress is closed.
2. **Prompt curation + classifier gate** — what the worker is _told_. The residual leak
   channel is Haku writing personal data into a prompt; the Anthropic classifier gate
   catches that, and a slip leaks one curated paragraph, not a context window.

| Principal                     | Provider     | Namespace          | May appear in context                                                                                                                                      | Typical work                                                                     |
| ----------------------------- | ------------ | ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Haku (orchestrator)           | Anthropic    | `haku-sandbox`     | everything (full read-only credential set)                                                                                                                 | synthesis, judgment, item authoring, all raw personal sources                    |
| oai-zone workers              | OpenAI       | `haku-sandbox-oai` | moderate personal context in curated prompts — project/calendar-shaped facts, coarse finances; **no** credentials, figures, identifiers, documents, health | hard coding, personally-framed research, judge/verifier zone for zai-zone output |
| zai-zone workers              | z.ai GLM     | `haku-sandbox-zai` | public-by-construction prompts only                                                                                                                        | ducktape chores, Grocy management, generic research                              |
| local-zone workers (deferred) | local Ollama | (deferred)         | sensitive-but-mechanical (zero external egress)                                                                                                            | bulk mail classification/summarization                                           |

The exact oai-zone prompt line is a values call for the operator; seed conservatively
(coarse context yes; figures/identifiers/documents/health no) and tune from feedback.

**Terminology — zone.** A **zone** is the concrete per-provider infrastructure track
workers run in: the namespace + its egress perimeter + the harness + the Job templates
that run there (the shared workers-LiteLLM enforces the zone's model set via each
per-job key's allowlist). Trust level is a property of the zone — the rows above define
what may enter a worker's context in each zone, and the zone's perimeter is what
enforces it. Jobs are dispatched _to a zone_. The deferred local zone would get its own
track, and two zones at one trust level (two equally trusted providers) is a valid
future shape.

## Architecture

```text
Haku orchestrator (Anthropic; runtime unpinned; in-session subagents included)
      │  POST /jobs {prompt, zone, model, budget}   reads jobs/results via haku_reader
      ▼
haku-dispatch namespace (ducktape-reviewed; Haku can call, cannot modify)
  dispatcher ── classifier → mint per-job key → stamp Job ── jobs+results in ─┐
  workers-LiteLLM (holds both zone keys as upstream creds)  one CNPG cluster ─┘
      ▲ CNP: only zone-namespace pods reach workers-LiteLLM (never Haku)
      │
haku-sandbox-zai                    haku-sandbox-oai
  workers hold per-job keys only      workers hold per-job keys only
  (zone-model-allowlisted)            (zone-model-allowlisted)
  no Google/Plaid egress              no Google/Plaid egress
  harness (zai: Claude Code CLI · oai: Codex CLI)
      └─per-job key─▶ workers-LiteLLM ─▶ main LiteLLM ─▶ z.ai | chatgpt

sensors: changedetection.io (LLM rulesets via LiteLLM) ──webhook──▶ haku-state intake/
```

### Components

- **LiteLLM is the keystone** (`cluster/k8s/litellm/`). GLM 4.5–5.2 are already
  registered against z.ai's Anthropic-shape endpoint (deliberate: GLM's OpenAI shape
  has a union-tool-input bug — see `docs/zai_api.md`). Two additions:
  - **Virtual keys** (the TODO already in `app/deployment.yaml`): enable the DB, mint
    per-zone keys with model allowlists + budgets + kill-switch. Today a single master
    key serves everyone; that must land first.
  - **`chatgpt` provider** (native in LiteLLM since 2026-01): device-code login once,
    auto-refreshing auth state on a PVC, exposes `chatgpt/gpt-5.x[-codex]` models. The
    ChatGPT subscription credential then lives **only in LiteLLM** — workers get virtual
    keys, same shape as the GLM zone. Fallback if LiteLLM's client trips Cloudflare
    (open bug, environment-dependent): run CLIProxyAPI or icebear/codex-proxy in-cluster
    as LiteLLM's upstream (see _Options_).
- **Dispatcher**: a small ducktape service. `POST /jobs` → classifier → on pass,
  create a k8s `Job` from a reviewed template into the zone's namespace;
  `POST /jobs/<id>/result` for turn-in; `DELETE /jobs/<id>` to kill — no GET surface.
  Kubernetes **is** the job→execute framework (retries, TTL
  cleanup, quota); job records + result blobs live in the dispatcher's **own small CNPG
  Postgres** (operator, 2026-07-02 — preferred over Forgejo/ConfigMap storage: no 1MB
  caps, no slow forge API in the loop, and a natural home for history, dedup, and
  future quota-queueing; ORM per house style). Haku reads results straight from the
  jobs/results tables with its read-only `haku_reader` role during orient — the
  dispatcher holds **no haku-state credential at
  all**, one fewer secret in the trusted middle. Credential placement is the
  load-bearing part: Haku holds only the dispatcher endpoint; the ServiceAccount that
  can create Jobs lives only with the dispatcher.
- **Worker namespaces**: stamped copies of the `haku-sandbox` perimeter
  (`cluster/k8s/agents/haku-mitmproxy/` CCNP/CNP + Kyverno inject + trust bundle +
  RBAC/quota, ~15–20 mechanical files each) minus the Google FQDNs in the CNP and minus
  every sensitive secret reflection (`google-access-token` ESO targets namespaces by
  explicit list; simply don't add these). Each zone gets its own principal via the
  existing `authentik-jwt-rotation` + explicit group-allowlist machinery
  (`agent-box-codex` is the precedent for a diagnostics-only, no-writable-namespace
  principal).
- **Worker harness v1 — shape-matched per zone, one image**: each zone's harness speaks
  its provider's native wire shape end-to-end, so no LiteLLM shape-bridging is ever on
  the hot path. **zai zone: Claude Code CLI** (Anthropic shape; GLM is registered
  Anthropic-shape precisely to dodge its OpenAI-shape bug; the `z-claude` wrapper in
  `nix/home/home.nix` proves it end-to-end). **oai zone: Codex CLI** (Responses shape,
  which the `chatgpt` provider forwards natively — the provider's open bug lives in the
  chat-completions↔responses bridge, which this avoids by construction; Codex points at
  the workers-LiteLLM via a `config.toml` `[model_providers]` entry). One worker image
  carries both CLIs; the Job template selects the harness by zone. The workers-LiteLLM
  serves both native shapes; verifying two-hop `/v1/messages` + streaming
  `/v1/responses` is the step-3 spike.
- **Results**: uniform turn-in via the dispatcher — see _Job lifecycle_. Langfuse (via
  LiteLLM callbacks) holds the token-level record with per-key attribution.
- **Sensors**: deploy changedetection.io ≥0.55.1 — it natively evaluates plain-English
  conditions per watch via LiteLLM ("notify only when the price drops below $50") and
  fires Apprise webhooks with diff payloads. One Deployment + `sockpuppetbrowser`
  sidecar for JS pages. The judging model routes through our LiteLLM, so even sensor
  judgment stays under our control. Bespoke sensor agents only where judgment is richer
  than a page-diff condition. Because sensors take no prompts and only emit findings,
  they can run at the lowest trust level; their output is untrusted input to Haku (which
  already treats all source data as potentially adversarial).

### New-code inventory

The complete bespoke surface, after every simplification in this plan:

1. **Dispatcher** (~400–700 lines Python + manifests): `POST /jobs` = auth +
   identifier lint + one classifier LLM call + mint per-job key on the workers-LiteLLM +
   stamp Secret/Job from the reviewed template (idempotent via Job name) + insert the
   job row; `POST /jobs/<id>/result` = verify HMAC token + store the blob in the
   dispatcher's CNPG Postgres; `DELETE /jobs/<id>` kill. **No GET surface** (operator,
   2026-07-02): Haku reads the jobs/results tables directly with a read-only Postgres
   role (`haku_reader`, a member of `pg_read_all_data` — a CNPG managed role, fully
   declarative; credential reflected into haku-sandbox) — full SQL filtering for free,
   one less API to maintain. SQLAlchemy ORM, two tables (jobs, results).
2. **Entrypoint wrapper** (~100–200 lines, in the worker image): read the prompt, point
   the zone's harness at the workers-LiteLLM, run headless, POST result + exit status.
3. **Classifier prompt + verdict schema** (reviewed like code; ~50 lines of threshold
   logic).

Deliberately absent because stock components cover it: queue/retries (k8s Jobs),
budget/allowlist/TTL enforcement (LiteLLM keys, two layers), per-job attribution (zone
LiteLLM → Langfuse), log redaction (unneeded under the CNP model), scheduling, any
haku-state write path (results are read from the jobs/results tables, not pushed as
intake).
Everything else in the plan is configuration: stamped perimeters, LiteLLM deployments +
generated configs, TF keys, CNPG clusters, changedetection.io, prose.

## Job lifecycle

The end-to-end path of one job. The invariant throughout: **workers never touch
haku-state in either direction** — git write implies read, so the moment a z.ai worker
could commit to haku-state it could read all of it; that channel does not exist.

1. **Dispatch (Haku → dispatcher).** `POST /jobs` with a **self-contained prompt** (the
   convention items' `prepared_prompt`s already follow) plus zone, model, and budget; a
   job that needs a repo is told in the prompt to `git clone` it. Self-containment is
   not a convenience — it is the mechanism that forces all context through the
   classifier gate, because the worker has no access to Haku's memory to go look things
   up.
2. **Gate + Job creation.** Deterministic identifier lint, then the Anthropic
   classifier; rejection returns the reason to Haku. On pass, the dispatcher stamps a
   Job from the ducktape-owned template: prompt mounted as a file, plus two
   dispatcher-minted credentials — a **per-job LiteLLM key** on the workers-LiteLLM
   (model allowlist,
   budget, TTL; see _Monitoring affordances_ → key containment) for the harness, and an
   HMAC **result token** for result submission scoped to its own job id.
   **Idempotency**: the Job's k8s name is
   derived from the caller's `idempotency_key` (`job-<hash>`), so a retried `POST /jobs`
   hits `AlreadyExists` in the API server and returns the existing job — exactly-once
   creation enforced atomically by k8s, no dispatcher bookkeeping.
3. **Workspace.** There is no workspace field on the job: a job that needs a repo is
   told in the prompt to `git clone` it — public ducktape for repo chores, nothing for
   Grocy work. No code path clones haku-state, and no
   credential in the namespace could. Worker pods run a **ServiceAccount with no k8s
   API grants** (and `automountServiceAccountToken: false`): a hijacked worker cannot
   read any secret via the API — not even its own zone's — only what is explicitly
   mounted into its pod.
4. **Execution.** The ducktape-owned entrypoint wrapper runs the harness headless
   (`claude -p`) on the job's prompt. What the job can _do in the world_ is fixed
   by the zone's capabilities (Forgejo push as the `haku-worker` bot, the write-scoped
   Grocy user, …), bounded by the perimeter — not by anything declared on the job.
5. **Turn-in — uniform, no deliverable types.** The agent writes `/output/result.md`;
   **after the agent process exits**, the wrapper POSTs it plus exit status to
   `POST /jobs/<id>/result` with the job-scoped token. A prompt-injected agent can at
   worst fabricate its own job's result content — already within its power. Anything the
   job delivered in the world (a pushed branch, an opened PR under its bot identity, a
   Grocy change) is ordinary capability use, merely referenced in the result text. A
   dispatcher-enforced deliverable taxonomy would constrain nothing (capabilities, not
   declarations, bound behavior) and is deliberately omitted. If zone-static
   capabilities ever prove too broad, the refinement is named **capability bundles** on
   the job spec (which secrets/mounts this Job gets) — a least-privilege capability
   list, still not a deliverable taxonomy.
6. **Delivery to Haku.** The dispatcher stores the result in its CNPG Postgres
   (operator, 2026-07-02 — Postgres over Forgejo/ConfigMap storage: no size caps, no
   slow forge API in the loop, natural home for history/dedup/queueing; and the
   dispatcher holds **no haku-state credential**). Haku queries the jobs/results tables
   with its read-only `haku_reader` role (`WHERE created_at > <bookmark>`) as part of
   orienting each run — the same bookmark discipline as every other source.
7. **Reduction (Haku, next run or wake).** For each completed job: accept and act (file
   an item, surface the PR), send a revision job (new prompt embedding the prior result;
   the branch persists for a follow-up worker), escalate (GLM botched it → re-dispatch
   to the oai zone), or drop. Outcome + Langfuse cost feed the calibration notes in
   state. **Worker output is untrusted input** — a report gets the same
   prompt-injection skepticism as an email body, and code deliverables are gated by CI
   and operator review regardless of what the report claims.

No persistent fleet at v1: each job is a one-shot k8s Job; parallelism = multiple Jobs
bounded by the namespace ResourceQuota plus the dispatcher's quota-awareness (queue or
re-route across the z.ai/ChatGPT windows rather than fail). Failures and timeouts
(`activeDeadlineSeconds`) surface as failed jobs at reduction. Multi-turn resumable
sessions (`claude --resume` with session state on a PVC) are a later refinement if
revision-by-new-job proves lossy.

## Monitoring affordances for Haku (operator, 2026-07-02)

Haku should be able to watch its fleet — job status, traces, spend — so it can
retry/escalate/kill intelligently instead of discovering failures only at reduction.

**Key containment: one shared second-layer "workers-LiteLLM"; workers hold only
per-job credentials whose model allowlist enforces their zone** (operator,
2026-07-02: stock LiteLLM over a bespoke proxy, and one instance shared across zones —
the per-job key's model allowlist is the zone boundary at the LLM layer, so the
instance doesn't need to be per-zone). The workers-LiteLLM runs in the
**`haku-dispatch` namespace** next to the dispatcher, chained to the main instance
(`litellm_proxy/` provider entries; the GLM model group authenticated with the zai
zone key, the chatgpt group with the oai zone key — only this deployment mounts them).
The dispatcher mints a **per-job virtual key** (`/key/generate`: that zone's model
allowlist, `max_budget`, TTL) at Job creation; the harness carries it as
`ANTHROPIC_AUTH_TOKEN`. The Job carries two dispatcher-minted credentials: the per-job
LiteLLM key (harness) and the HMAC result token (wrapper). Consequences, stated
honestly:

- **Exfiltration is worthless by construction — via reachability, not secrecy**: a
  CiliumNetworkPolicy admits only zone-namespace pods to the workers-LiteLLM, so a
  per-job key leaked through worker output cannot be used by Haku or anything outside
  the zones. (This is also why per-job keys can't simply be minted on the **main**
  LiteLLM: it is reachable cluster-wide, so a leaked key there would hand Haku a GLM
  path until TTL/budget ran out.) Cross-zone: the direction that would matter is an
  oai worker using a stolen zai-zone key to send oai-zone content to GLM — but no
  channel exists through which a key crosses zones (workers cannot query other jobs;
  results flow only worker→dispatcher), and every key stays allowlisted to its own
  zone's models regardless of who presents it. Residual accepted: sibling-worker theft
  within a zone — same trust, budget- and TTL-bounded.
- **Compartmentalization trade, accepted**: one process holds both zones' upstream
  keys (a LiteLLM compromise from a malicious worker request would expose both, vs.
  one with per-zone instances) — bounded by the main instance's per-zone budgets +
  kill-switches, and bought back as one deployment + one DB instead of N.
- **Two budget layers, both stock LiteLLM**: the main instance caps each static zone
  key (coarse zone budget + kill-switch); the workers-LiteLLM enforces per-job model
  allowlist + budget + expiry as native virtual-key semantics — no bespoke
  enforcement code at all.
- **Langfuse split falls out for free**: the workers-LiteLLM logs to the
  **`haku-workers` Langfuse project** via its own `LANGFUSE_*` env (key alias = job
  id → per-job attribution; zone in key metadata); the main instance keeps logging
  its other consumers to the main project. No per-key routing config anywhere.
- **Costs**: one key-DB (the `litellm_workers` database in the shared `haku-dispatch`
  CNPG cluster, next to the dispatcher's `dispatcher` database) with its own
  never-rotate salt key; +one proxy hop.
  **Spike before committing**: verify `/v1/messages` and `/v1/responses` (streaming —
  the chatgpt path is streaming-only) chain cleanly through two LiteLLM hops.
- **Fallbacks considered**: per-zone LiteLLM instances (restores upstream-key
  compartmentalization at N× the moving parts); generalizing `props/llm_proxy`
  (working code, but bespoke and duplicates stock LiteLLM features) if two-hop
  pass-through misbehaves; a per-pod localhost auth sidecar (Centaur's iron-proxy
  insight minus TLS-MITM) if a shared instance proves annoying.
- **Base hard rule** (add with the other doctrine amendments): Haku never uses a
  credential found in worker output; finding one means reporting it as compromised so
  it gets rotated.

The affordances:

- **Direct SQL reads (primary)**: Haku queries the dispatcher's jobs/results tables with
  the `haku_reader` read-only role (operator, 2026-07-02 — replaces a `GET /jobs` API:
  full SQL filtering for free, less dispatcher code). The tables contain only
  Haku-authored prompts and worker-authored results — no credential is stored in the
  dispatcher DB. `haku_reader`'s `pg_read_all_data` membership does span the
  workers-LiteLLM's `litellm_workers` database in the same cluster, but that exposes
  only sha256-hashed virtual keys and spend logs, never plaintext credentials
  (`store_model_in_db: false` keeps provider creds out of the DB entirely).
  `DELETE /jobs/<id>` on the dispatcher stays the kill switch — canceling its own
  dispatched work is within the subset-privilege rule.
- **Langfuse traces**: route zone-key traffic to a dedicated **`haku-workers` Langfuse
  project** (LiteLLM supports per-key Langfuse routing/metadata; the props
  `litellm_metadata` tagging pattern applies) and reflect a **viewer-scoped key** for
  that project into `haku-sandbox`. Worker traffic is non-sensitive by construction
  (gated prompts), so Haku reading its prompts/completions/costs is fine; a shared
  all-of-LiteLLM project would instead expose other consumers' traffic. The trace
  contains the harness's stream-json — every request/response, cost, timing — which is
  the highest-value "stuck/looping/burning budget" signal, largely superseding logs.
- **k8s object status is already covered**: the cluster-wide `cluster-diagnostics-reader`
  grant (no secrets, no logs, no configmaps) lets Haku watch Job/pod phase, exit codes,
  restarts, and events in the zone namespaces the moment they exist — no new RBAC.
  The rule that keeps pod specs safe to expose: **no secret values inline in
  env/args — `secretKeyRef`/file mounts only** (house style already).
- **Raw worker logs: skipped at v1, pre-approved in principle** (operator, 2026-07-02).
  Traces + status/events + the result blob cover v1; but the case traces answer poorly
  is "harness stuck/crashed vs. just waiting on a long job" (a wedged harness emits no
  LLM calls — indistinguishable from thinking; wrapper stderr shows it immediately). If
  that need materializes, grant Haku `pods/log` in the zone namespaces — one
  `RoleBinding`
  per zone on the `logs-configmaps-reader` pattern, no code. The risk that originally
  argued against this is gone under the zone-LiteLLM design: the only credential that
  can appear in worker logs is a per-job key, unusable from Haku's namespace by CNP; the
  zone-instance ingress policy is the secondary defense that makes log access safe.

Everything these surfaces return is **worker-authored, i.e. untrusted input** to Haku —
same skepticism as results. Traces are for operational judgment (stuck? loop? budget
burn?), not for trusting claims.

## What each zone can take (initial routing)

- **zai zone (GLM)** — prompts public by construction:
  - ducktape work (public repo): the infra/coding backlog from the delegation register
    (Linode→OVH, Harbor→zot, in-cluster ActivityWatch, owncloud/nextcloud trial), lint
    and dependency churn, CI babysitting. CI + PR review + operator merge absorb GLM's
    quality variance.
  - Grocy management: pantry data is low-sensitivity and Grocy's per-user ACL is the
    boundary (Haku's read-only user 403s writes server-side). Give the _worker_ a second
    Grocy user with write perms scoped to stock + shopping lists — upstream-enforced, no
    facade needed.
  - Generic research where the question needs no personal framing ("compare nextcloud
    vs owncloud for a small k8s cluster").
- **oai zone (OpenAI)** — the middle trust level the original two-level design lacked:
  - harder coding that GLM botches (instead of escalating to Haku),
  - research needing mild personal framing ("ACA plans in SF for a single adult with
    these rough parameters"),
  - a judge/verifier zone reviewing zai-zone output before it reaches Haku
    (cross-provider diversity is a feature).
- **Never in a worker zone**: anything reading raw Gmail/Drive/Plaid/Tana content;
  haku-state work
  (the repo is the personal-data motherlode — consider splitting `ui/` into its own repo
  before offloading frontend chores); final synthesis and operator-facing voice.
- **Grocery orders** (future): the gmail-labeling closure-invariant pattern — a small
  reviewed MCP server holding the vendor credential server-side, exposing only bounded
  ops (`add_to_cart`, `submit_order` under a $-ceiling against a merchant allowlist).
  Once that exists, even an untrusted worker can operate it safely. Generalization:
  **bounded-write MCP servers are how low-trust agents get safe write capabilities.**

## Ownership split and doctrine changes

- **Ducktape (operator-reviewed; Haku proposes via Forgejo PRs):** dispatcher +
  classifier prompt, worker images and Job templates, namespace perimeters, LiteLLM
  config/keys, any "X must never contain PII"-style automated check. Letting Haku change
  these without review would let it bypass its own gate.
- **haku-state (Haku-owned):** routing policy (`procedures/`), per-model capability
  calibration ("GLM does dependency bumps at ~4% of Opus cost with high acceptance;
  fails at Bazel refactors"), the delegation register as an active dispatch queue,
  outcome/cost feedback folded from Langfuse.
- **Base amendments needed** (edit `haku/base/instructions.md` when building):
  1. **Autonomous dispatch rule**: Haku may dispatch work to principals whose privileges
     are a strict subset of its own and whose spend is capped by construction;
     operator-gated launch remains required at or above Haku's own privilege.
  2. **Per-zone prompt floor**: enumerated categories that never enter a worker-zone
     prompt (credentials, account numbers, figures, identifiers, documents, health),
     with the live rubric in state.
  3. **PR authorship**: record Forgejo-ducktape PRs as a sanctioned bounded write.
- **Prerequisites for Haku's ducktape PRs**: the cluster-Forgejo ducktape mirror must
  become an automated mirror (today it is bumped manually — see
  `haku/runtime/managed_agent/self_hosted/README.md`), and the `haku` Forgejo user needs
  fork/branch/PR rights. A PII/classifier check can run as a required status check on
  worker- and Haku-authored PRs (Forgejo required contexts glob-match
  `workflow / job (event)` strings), as belt to the dispatch gate's suspenders.

## Options considered

### Dispatch plane

Research verdict (five independent surveys, 2026-07-02): **no existing system ships
"job → automatic classification gate → execute" as a first-class feature** — the one
purpose-built pre-action approval library (HumanLayer) was deprecated in a pivot;
everything else gates by human assignment or post-hoc PR review. But the gate composes
with any plane as a thin dispatcher in front of the submission API, so the plane is
chosen on ops merits alone:

| Option                                                     | Verdict                                                                                                                                                                                                                                                                                                                                          |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **plain k8s Jobs behind the dispatcher** ✅                | Chosen. Zero new components; Job objects are the run record; quotas/TTL/retries built in.                                                                                                                                                                                                                                                        |
| Argo Workflows                                             | The upgrade path if we want DAGs/per-step retries/runs UI: the same dispatcher submits `Workflow` CRs instead (gate stays _in front of_ submission, not inside a template, so it can't be edited out). Argo Events and Kueue: skip (4+ standing pods / admission-quota machinery for what one HTTP call does).                                   |
| Forgejo Actions                                            | Demoted from dispatch plane to CI: `workflow_dispatch` is solid (typed inputs; Forgejo returns run id + jobs on fire), but the runner has no pod-per-job executor (privileged dind only; native k8s backend is prototype-stage 05/2026) — running agent workloads inside CI would mean privileged dind. CI keeps image builds + required checks. |
| Temporal / Windmill / n8n / Prefect / Kestra               | None clears the bar at single-operator scale; Temporal is the most ops for the least incremental value here; Windmill's audit/git features are EE; n8n is JSON-blob workflows (license fair-code). Prefect is the closest call if we ever outgrow CI+Jobs.                                                                                       |
| Centaur (Paradigm)                                         | Closest existing system; evaluated in depth from source — see _Centaur — deep evaluation_ below. Verdict: don't adopt (no zone axis; it owns the same perimeter layer our Cilium/Kyverno stack owns); steal its durable-execution API design for the dispatcher.                                                                                 |
| Anthropic Managed Agents (self-hosted)                     | 1:1 architecture match (Anthropic queues sessions; our worker spawns a pod per session) **but the loop runs Anthropic models only** — cannot drive GLM/OpenAI zones. Stays relevant only as a Haku runtime option (Runtime B).                                                                                                                   |
| Vendor clouds (Codex cloud, Jules, Cursor, Devin, Copilot) | All fail self-host and/or our-forge requirements. Reference: Jules is the only vendor with an API-level plan-approval gate (`requirePlanApproval`); Cursor self-hosts only workers while the loop stays in their cloud.                                                                                                                          |

Useful design references: Beads' ready-queue + atomic-claim semantics for a future job
table; `coder/agentapi` (MIT, one Go binary) to drive/observe a CLI harness in a pod
over HTTP+SSE; `kubernetes-sigs/agent-sandbox` CRDs + gVisor (Talos system extension)
as a later isolation upgrade.

### Centaur — deep evaluation (2026-07-02, from source)

[Centaur](https://github.com/paradigmxyz/centaur) (Paradigm + Tempo; Apache-2.0 OR MIT;
open-sourced 2026-05 after internal production since 2026-01; release-per-merge cadence,
~800★) is the closest existing system to this plan's shape, so it was read from source
before committing to the bespoke dispatcher.

**What it is** (verified in-repo): a Helm chart deploying a Rust/Axum control plane
(`api-rs`), Postgres (paradedb), a Slack bot (on by default; Teams/Discord/Linear
optional), and the `kubernetes-sigs/agent-sandbox` controller (v0.4.6) — sandboxes are
`Sandbox` CRs with a warm pool (3 pre-booted by default). Each sandbox gets a dedicated
**iron-proxy** MITM pod injected via `HTTPS_PROXY`: the sandbox holds only placeholder
strings; the proxy substitutes real credentials at the wire, bound to specific hosts
(Anthropic/OpenAI/GitHub headers, Bedrock SigV4 re-signing, OAuth brokering — including
Claude-subscription refresh tokens via iron-token-broker — even proxied Postgres DSNs).
Runs on k3s-class clusters; docs include a Mac-Mini/VPS setup.

**The headless path is exactly our contract**: `POST /api/session/{thread_key}` (harness
choice per session: claude-code / codex / amp / pi-mono) → `POST …/execute` with an
`idempotency_key` → SSE `…/events` replayable via `after_event_id` → terminal
`session.execution_completed {result_text}`; all events durable in Postgres. Claude Code
runs as stream-json pass-through with a `CLAUDE_SETTINGS_OVERLAY` deep-merged into
`~/.claude/settings.json` at pod start.

**What fights us** (all verified in source):

1. **The zone axis doesn't exist.** The sandbox namespace is hardwired to the Helm
   release namespace (`SESSION_SANDBOX_K8S_NAMESPACE: {{ .Release.Namespace }}`); the
   sandbox image and env are deployment-global; the session API exposes only
   `harness_type`/`persona_id`. Our core requirement — job → pinned
   namespace/image/secrets/perimeter — means one full Centaur install per zone, or
   forking `api-rs`.
2. **It insists on owning the perimeter.** `api-rs` dynamically creates per-sandbox
   NetworkPolicies and per-sandbox MITM proxies and injects `HTTPS_PROXY` + a proxy CA —
   a competing owner of exactly the layer our CiliumNetworkPolicy + Kyverno-injected
   mitmproxy already occupy. Chaining two MITM proxies/CAs is not a supported path, and
   Kyverno mutations on controller-created pods would race its warm-pool logic.
3. **Credential scoping is chat-principal- or deployment-shaped, not zone-shaped.**
   `ANTHROPIC_BASE_URL` (→ LiteLLM) can only be set deployment-wide; per-zone virtual
   keys don't map. iron-proxy solves the same problem as our LiteLLM virtual keys at a
   different layer (agent holds nothing vs. agent holds a scoped key) — for an
   LLM-only credential surface it duplicates what we already have; its distinctive value
   is wire-level injection of _tool_ credentials (GitHub, Postgres, OAuth'd SaaS), which
   our bounded-write-MCP pattern covers differently.
4. **Chat gravity and churn**: Slack secrets are boot requirements even if unused;
   1Password is the default secret source; 0.1.x API changing weekly; the recent Rust
   rewrite dropped capabilities (issues #621, #683) and docs drift.

**Verdict: don't adopt.** Centaur's unique value concentrates in layers this plan
already solves differently (perimeter → Cilium/Kyverno; credentials → LiteLLM virtual
keys) or deliberately keeps thin (the gate). **Steal for the dispatcher instead**: the
four-endpoint durable-execution API shape, `idempotency_key` on execute,
`after_event_id` event replay, terminal-`result_text` extraction from stream-json, the
settings-overlay config merge, and warm pools if cold-start latency ever matters. That
Centaur builds on `agent-sandbox` also validates those CRDs as our optional substrate.

**Re-checks in the same pass**: `agenttier` is structurally _closer_ to the zone model
(multi-namespace sandboxes; templates-as-profiles with per-template ServiceAccount;
`POST /invoke` SSE) but is bus-factor-1 with no visible adoption (55★) — watch, don't
build on. New single-operator reference found:
[netclode](https://github.com/angristan/netclode) (k3s + Kata microVMs, warm pool,
replayable event stream) — conversation-first, good microVM notes.

### OpenAI subscription access

The operator's premise verified: OpenAI de-facto tolerates subscription credentials
outside the official Codex harness (public endorsement by OpenAI's Head of Developer
Experience; opencode ships native ChatGPT sign-in unchallenged; no bans reported for
personal proxy use). The community line: one subscription, your own workers, no
pooling/reselling. Mechanism shared by all options: Codex CLI's OAuth
(`auth.json` with auto-refresh; ~8-day client refresh cadence; quota returned via
`x-codex-primary-*`/`x-codex-secondary-*` headers).

| Option                                   | Notes                                                                                                                                                                                                                                                                                                                                                                                                        |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **LiteLLM native `chatgpt` provider** ✅ | First choice: zero new components; credential stays in LiteLLM; virtual keys give attribution/budget/kill-switch. Open bugs: Cloudflare-403 (TLS-fingerprint class, environment-dependent — test from cluster egress) and a responses↔chat-completions bridge bug — structurally avoided by the oai zone's harness being **Codex CLI**, which speaks the Responses shape natively (see _Worker harness v1_). |
| CLIProxyAPI                              | Fallback upstream behind LiteLLM: most maintained (~39k★, daily pushes), Go/Docker/headless, multi-account, management API. Large attack surface + gray-market sponsor links — pin versions, restrict egress.                                                                                                                                                                                                |
| icebear/codex-proxy                      | If we specifically want an Anthropic-compatible `/v1/messages` surface for codex models + first-class per-account quota reporting.                                                                                                                                                                                                                                                                           |
| ChatMock                                 | Simplest single-account fallback; slower-moving.                                                                                                                                                                                                                                                                                                                                                             |

Operational: persist the auth dir on a PVC and alert on refresh failure; expect the 5h +
weekly windows to be the real capacity constraint (Plus is thin for fleet use — Pro or
the 5× tier if this becomes a real zone); dispatch should be quota-aware (extend
`aiquota` with an OpenAI provider next to the existing z.ai one).

### Worker harness

| Option                                             | Verdict                                                                                                                                                                                                       |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Claude Code CLI → LiteLLM (Anthropic shape)** ✅ | v1 for both zones: proven by `z-claude`; one image; `--bare`/headless mode is first-class; per-zone model via virtual key.                                                                                    |
| Runtime C loop (`haku/runtime/agent/`, MAF)        | Feature-complete but undeployed; speaks OpenAI chat shape to LiteLLM, and the GLM entries are registered Anthropic-shape — translation path untested. Long-term provider-agnostic option, not the v1 blocker. |
| Codex CLI                                          | Natural if a zone ever talks to OpenAI directly (native subscription auth, MCP support); unnecessary once LiteLLM fronts the subscription.                                                                    |
| `coder/agentapi` wrapper                           | Optional layer for driving/observing whichever CLI runs in the pod; adopt if the dispatcher wants streaming introspection.                                                                                    |

### Sensors

| Option                                   | Verdict                                                                                                                                                                                                          |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **changedetection.io ≥0.55.1** ✅        | Mature, Apache-2.0, LLM rulesets via LiteLLM, Apprise webhooks, community Helm charts; LLM features are ~2 months old — expect some churn.                                                                       |
| firecrawl-observer                       | Cleanest small reference implementation (diff → LLM meaningfulness score → threshold → webhook) but Convex/Firecrawl-SaaS-tied.                                                                                  |
| Bespoke git-scrape + CI cron + LLM judge | Well-precedented pattern (whale-spotter et al.), never templated; reserve for watches where diff history itself is valuable. Forgejo Actions `schedule:` works (DST quirk; no GitHub-style 60-day auto-disable). |
| Skyvern / browser-use                    | Heavyweight browser agents; only if a watch needs real interaction.                                                                                                                                              |

## Considerations and risks

- **Residual leak channel = prompt content.** Perimeter can't check what Haku writes
  into a prompt; that's the classifier gate's job, plus the per-zone floor in base and
  a cheap deterministic lint (known identifiers/account fragments) inside the
  dispatcher before the classifier even runs. A slip leaks one curated paragraph.
- **Prompt injection into workers** (GitHub issues, web pages are carriers): blast
  radius = the worker namespace + whatever bounded-write MCPs it holds; its PRs go
  through CI + review. Workers get their own bot identity so provenance is attributable.
- **Quota exhaustion / rate limits**: z.ai has 5h/7d windows (already monitored by
  `aiquota`); ChatGPT subscription has 5h + weekly. The dispatcher should check
  remaining quota before creating a Job and queue or re-route (zai→oai→defer) instead
  of failing.
- **LiteLLM `chatgpt` Cloudflare risk** is environment-dependent — spike it from the
  cluster's egress IP before building the zone; fall back to CLIProxyAPI upstream.
- **Subscription token custody**: the ChatGPT OAuth token is the operator's personal
  account. Keep it exclusively in LiteLLM (or the fallback proxy) — a June 2026 npm
  supply-chain attack specifically harvested `auth.json` tokens from developer machines;
  centralizing is the mitigation.
- **Anthropic subscription asymmetry**: Anthropic restricts subscription OAuth to its
  own harness, so Haku's subscription economics only survive on Runtime A/B — one more
  reason the runtime stays unpinned rather than migrating Haku to the self-hosted loop.
- **Don't let Haku own gate-adjacent infrastructure.** Worker images, Job templates,
  and checks stay in ducktape; Haku's GitOps self-deploy path
  (`haku-state-workload-deployer`) stays scoped to `haku-sandbox` and does **not**
  extend to the worker namespaces.
- **haku-state privacy**: never clone haku-state into worker zones. If UI chores should
  become offloadable, split `ui/` into its own repo first.

## Build order

1. **LiteLLM virtual keys — ✅ LANDED** (PR
   [#2739](https://github.com/agentydragon/ducktape/pull/2739), merged 2026-07-02):
   `litellm-db` CNPG cluster (OVH-HA) + `DATABASE_URL`; `LITELLM_SALT_KEY` minted by
   `tf/gitops/litellm-api-key` with `prevent_destroy` (never rotate);
   `store_model_in_db: false` in the generated config (models stay parity-tested config
   — sidesteps [#28044](https://github.com/BerriAI/litellm/issues/28044); TF model
   matrices remain the end-state gated on that bug + verified `os.environ/` refs in DB
   models); new `tf/gitops/litellm-keys` module
   ([ncecere/litellm](https://github.com/ncecere/terraform-provider-litellm) 2.0.1,
   pinned into the `rules_tf` mirror with a supply-chain caution) minting
   `haku-lane-{zai,oai}` keys ($25/30d, model allowlists) as secrets whose reflector
   annotations already target the future zone namespaces. (These merged artifacts —
   `key_alias`, secret names `litellm-key-haku-lane-*`, TF `metadata.lane` — carry the
   pre-rename "lane" term; rename to `-zone-` opportunistically in a small follow-up
   or live with it.) **Deliberately deferred**: Haku and dispatcher-classifier keys (no
   Anthropic models registered; Haku must never be GLM-allowlisted). **Residue still
   open**: the `haku-workers` Langfuse project + per-key routing + viewer key (see
   _Monitoring affordances_); verify at rollout that the 1.90.2 image ran the Prisma
   migration and the `litellm-keys` Terraform CR reaches `Applied`.
2. **`chatgpt` provider — ✅ LANDED** (independently on devel): provider + auth-seed +
   PVC wired in `cluster/k8s/litellm/`; only the three Codex-backend models serve, and
   **streaming-only** (the request-shape caveat, documented in `generate_litellm.py`) —
   which the oai zone absorbs by using Codex CLI as its harness. Residue: an end-to-end
   Codex-CLI-against-LiteLLM smoke test, naturally part of worker bring-up (step 3/5).
3. **Zone + dispatch infrastructure — 🔄 IN REVIEW** (PR
   [#2748](https://github.com/agentydragon/ducktape/pull/2748), CI green): (a)
   `haku-sandbox-zai` perimeter: namespace, quota/limits, no-grants worker SA, shared
   `haku-zones-mitmproxy` (haku-mitmproxy pattern minus the Google FQDNs; CCNP with NO
   `toEntities: cluster` and NO kube-apiserver — much tighter than haku-sandbox's
   fence), Kyverno inject under the zone's own kustomization tree
   (`haku/zones/policies/`); (b) `haku-dispatch` namespace: **workers-LiteLLM** (config
   generated by `generate_workers_litellm.py` importing the main generator's GLM list —
   parity-tested, plus a cross-check against the TF zone-key allowlist), CNP admitting
   only zone pods + the dispatcher, CNPG `haku-dispatch-db` + salt/master-key TF.
   Residue: the two-hop native-endpoint streaming spike at rollout.
4. **Dispatcher v0 + worker image — 🔄 IN REVIEW** (PR
   [#2754](https://github.com/agentydragon/ducktape/pull/2754), stacked on #2748):
   dispatcher (`haku/dispatch/`: lint → classifier → per-job key mint → Job stamp;
   result turn-in; `DELETE /jobs/<id>` kill), worker image
   (`ghcr.io/agentydragon/haku-zone-worker`: Claude Code CLI + Codex CLI + git,
   stdlib-only entrypoint) + reviewed Job template (configMapGenerator; `${...}`
   placeholders; parity-tested from `haku/dispatch/test_k8s_jobs.py`); the
   `haku-dispatch-db` CNPG cluster hosts the `dispatcher` and `litellm_workers`
   databases (the repo's first CNPG `Database` CRs) + managed roles `dispatcher` and
   `haku_reader` with ESO-generated passwords. The operator-driven redesign is in: the
   API is `POST /jobs`, `POST /jobs/<id>/result`, `DELETE /jobs/<id>` (no GET surface),
   the job input is `JobRequest.prompt`, and Haku reads the jobs/results tables via
   `haku_reader` (member of `pg_read_all_data`, fully declarative). Residue: first
   prompts = two or three ducktape chores from the Tana backlog, end to end, at
   rollout.
5. **`haku-sandbox-oai`** — one more perimeter namespace + a CNP entry on the
   workers-LiteLLM + chatgpt models in its config; same image, Codex CLI harness.
6. **Sensors + affordances** — changedetection.io + webhook→intake; Forgejo ducktape
   mirror automation + `haku` PR rights; base doctrine amendments (dispatch rule, prompt
   floor, PR authorship, found-credential rule).

Deferred: local-GPU zone; `agent-sandbox`/gVisor isolation; grocery-order bounded-write
MCP; PII check as required CI status on PRs.

## Open questions (operator)

- Which ChatGPT plan carries the oai-zone load (Plus windows are thin for fleet use)?
- Exact oai-zone prompt line — is "employer + rough equity situation" acceptable in an
  OpenAI research prompt, or does anything financial stay with Haku?
- When (if ever) to split `ui/` out of haku-state to make UI chores offloadable.

## References

Session research (2026-07-02), key external sources: LiteLLM `chatgpt` provider
([docs](https://docs.litellm.ai/docs/providers/chatgpt),
[PR #19030](https://github.com/BerriAI/litellm/pull/19030); open bugs
[#27175](https://github.com/BerriAI/litellm/issues/27175),
[#25429](https://github.com/BerriAI/litellm/issues/25429)); Codex auth/token lifecycle
([openai/codex](https://github.com/openai/codex));
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI);
[icebear/codex-proxy](https://github.com/icebear0828/codex-proxy);
[ChatMock](https://github.com/RayBytes/ChatMock);
[changedetection.io](https://github.com/dgtlmoon/changedetection.io) (LLM rulesets since
0.55.1); [firecrawl-observer](https://github.com/firecrawl/firecrawl-observer);
[coder/agentapi](https://github.com/coder/agentapi);
[kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox);
[Centaur](https://github.com/paradigmxyz/centaur);
[Beads](https://github.com/steveyegge/beads); Anthropic's parallel-agent harness
writeup ([building a C compiler with parallel Claudes](https://www.anthropic.com/engineering/building-c-compiler)).
