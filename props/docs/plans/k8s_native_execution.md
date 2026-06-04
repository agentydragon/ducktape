# Plan: k8s-native agent execution

**Status:** in progress (Stage 1 landed). Splits the agent data plane from the
dashboard control plane and replaces most of the bespoke container-orchestration
layer (`props/orchestration/`) with native Kubernetes patterns — while **keeping** a
slim reconcile-from-API controller to create agent pods (the cred boundary in Stage 5
requires it).

## Why

The orchestration layer reimplements a Kubernetes controller — in-memory
desired/actual reconciliation, manual restart-on-image-change, manual
kill/cleanup — but without the parts that make controllers robust
(reconcile-from-API, `ownerReferences`/GC, rollout semantics). The tax has been
real and recurring:

- Graders held actual-state in memory, so every backend rollout started a fresh
  generation and orphaned the previous one into duplicate pods per snapshot
  (no `ownerReferences`, no reaping). #1882 fixed this by making the controller
  reconcile from the k8s API (one labeled grader per snapshot; adopt/reap) — the
  robustness a `Deployment` would have given for free.
- Agents are coupled to the dashboard backend: every LLM call is proxied through
  it, so rolling the API server can disrupt in-flight agents.

The fix is **not** to hand pod lifecycle to k8s `Deployment`s/`Job`s: a security
boundary — _privileged creds never enter any agent pod_ (see Stage 5) — requires
the controller to create each pod so it can inject ephemeral per-run creds. Instead
we keep that controller but make it a proper reconcile-from-API controller (#1882,
done) and split the agent **data plane** (the LLM proxy + DB) from the dashboard
**control/read plane** (API + frontend), so the disposable part can roll freely.

## Target architecture

**Data plane (agents depend on this; roll carefully):**

- **LLM proxy** — standalone Deployment + `py_binary`. Owns auth (per-agent
  Postgres role), tree-aware budget enforcement, cost recording, and transcript
  capture. Agents' `OPENAI_BASE_URL` points here, not at the dashboard.
- **PostgreSQL** (unchanged) — the eval data model + RLS + drift `LISTEN/NOTIFY`.
- **Forgejo registry** (unchanged) — agents pull images directly in-cluster.

**Control/read plane (rollable without disrupting agents):**

- **API server + frontend** — dashboard, run/issue browsing, and the
  control endpoints agents call to request work (e.g. critic_dev launching a
  critic). Also hosts the **registry proxy for external CI push** only.
- **Orchestration controller** — the one irreducible custom piece: reconciles
  the snapshot set ↔ grader `Pod`s (the #1882 reconcile-from-API loop) and
  creates critic `Pod`s on request. Holds the only cluster-write RBAC **and** the
  admin DB creds, so it — never an agent pod — mints each pod's ephemeral per-run
  role. Pod creation lives here precisely to keep _privileged creds out of agent
  pods_ (see Stage 5).

**Workloads:**

- **Critics → bare `Pod`s** (`restartPolicy: Never`, `activeDeadlineSeconds` for
  timeout). The controller + DB `AgentRun` are the source of truth; a `Job`
  wrapper would only add `ttlSecondsAfterFinished` (cleanup if the controller is
  down) — adopt later if wanted. No auto-retry (each run is one eval data point
  and costs LLM spend).
- **Graders → controller-managed bare `Pod`s** (one per snapshot), _not_
  `Deployment`s — see Stage 5 for why (the cred boundary needs host-created pods).
  The #1882 reconcile loop keeps exactly one healthy grader per snapshot and reaps
  the rest. **Graders still have runs:** a _run is a pod generation_ — but the
  **controller** (not the pod) opens a new `AgentRun` + per-run role each time it
  spawns/replaces a grader, so "restart from fresh context" and "new image" each
  produce a new run, with the same ephemeral per-run creds critics get.

## Logs & transcript

Two distinct things, both readable by critic_dev for the agents it launches:

- **Transcript** (LLM turns / tool calls / findings) — the **LLM proxy** already
  writes one `llm_requests` row per call (full request + response, keyed by
  `agent_run_id`), so the transcript is structured per-turn rows in the DB,
  RLS-scoped, surviving pod deletion. critic_dev reads it via
  `GET /api/runs/{id}/llm_requests` (Stage 2).
- **Raw stdout/stderr** (tooling/container issues) — shipped to **Loki** by
  Promtail. Exposed to agents through a scoped `GET /api/runs/{id}/logs`
  endpoint that queries Loki by the run's pod label and **enforces RBAC**:
  authorized by run lineage — the caller may read a run iff it is an ancestor of
  that run (or admin). This reuses the existing `parent_agent_run_id` tree that
  already backs budget enforcement (`agent_run_budget_status` sums the same
  descendant set), so it's not new machinery. Promtail promotes
  `adgn.agent_run_id` to a Loki stream label; the dashboard links humans to
  Grafana by the same label.

This deletes #1870's "copy `container_stdout` into Postgres" approach: status
lives in the DB, logs live in Loki, transcript lives in the DB via the proxy.

## "Current grader" pointer

Flux **image automation** watches the grader repo in Forgejo (an
`ImageRepository` + `ImagePolicy`) and writes the current digest into a
`ConfigMap`; the **controller** reads it and reconciles grader pods onto the new
digest (it already reaps wrong-image pods, #1882). This replaces the registry-proxy
`pg_notify` (`grader_definition_changed`) + builtin-tag mechanism entirely. (There
is no grader `Deployment` for Flux to roll — the controller rolls the pods, for the
cred-boundary reason in Stage 5.)

Grader-only: critics have **no** "current" pointer — each critic run names an
explicit image digest chosen by the optimize loop from DB fitness.

## Staged implementation

Each stage is independently shippable and valuable; later stages depend on
earlier ones only where noted.

### Stage 1 — Split the LLM proxy out

- Extract `props/backend/routes/llm.py` (+ auth/budget/cost helpers) into a
  standalone `py_binary` + `oci_image` + Deployment + Service.
- Repoint agents' `OPENAI_BASE_URL` at the proxy Service.
- Capture the full transcript (request + response) keyed by `agent_run_id` at
  the proxy — **deferred to Stage 2** (it pairs with the transcript endpoint).
- **Done when:** rolling the API server does not interrupt an in-flight agent's
  LLM calls. ✅ Met — agents talk to the `props-llm-proxy` Service, not the
  dashboard.
- **Risk:** low. Pure extraction of an existing chokepoint.
- **TODO — split the config.** The proxy needs only `upstreams` (+ DB and the
  upstream API-key envs). `backend_url`, `grader_model`, `executor`,
  `auto_migrate`/`auto_sync_specimens`, `agent_env`, `models`, and the specimens
  are API-server-only — and the proxy reads model routing from the DB
  `ModelMetadata` table (synced by the API server), not `config.models`. The
  initial split reuses the full `PropsConfig` and reads only `.upstreams`; follow
  up by carving `PropsConfig` into shared / proxy-only / api-only shapes so each
  service gets a minimal config (the proxy ideally just `upstreams`), and mount
  the proxy a slimmer `config.toml`.
- **Status (2026-06-04): landed and live.**
  - #1886 — standalone proxy app + `oci_image` + deploy (Deployment + Service +
    Flux image automation + `push-images` row).
  - #1892 — fix the proxy CLI so `serve` is a real subcommand (the container
    crash-looped otherwise).
  - #1890 — cutover: agents' `OPENAI_BASE_URL` derives from `llm_proxy_url` (the
    `props-llm-proxy` Service), with the backend `/v1` kept as a fallback.
  - This PR — drop the backend `/v1` mount now that agents are on the proxy.
  - Tangential fixes the rollout surfaced: graders now reconcile against the k8s
    API (#1882) and the executor SA can `list` pods (#1891), so the grader
    controller maintains one labeled grader per snapshot.

### Stage 2 — Agent-readable logs (Loki)

- **Transcript stays DB-direct — no endpoint.** The proxy already logs each
  request+response to `llm_requests`, and `GET /api/runs/{id}/llm_requests`
  (RLS-scoped) already returns it; agents read their descendants' transcripts
  straight from the DB.
- **Logs:** `GET /api/runs/{id}/logs` queries Loki by the run's pod label
  (`{namespace="props",pod=~".+-<run_id[:8]>"}`) — promtail already labels `pod`,
  so no relabel is needed. **RBAC is RLS:** the run is looked up via the caller's
  RLS-scoped DB (the `agent_runs_select_descendants` policy), so an agent sees
  only runs it launched and admin/evaluator see all. Container logs are no longer
  persisted to the DB — Loki is the store.
- Added the props backend to the Loki `NetworkPolicy` ingress allowlist.
- **Status (2026-06-04): logs endpoint + Loki client landed.** Done when a
  critic_dev agent fetches a launched critic's logs via the endpoint.
- **TODO — namespace-isolate agent pods from the backend.** Agent pods (critic,
  grader) currently share the `props` namespace with the backend + DB. Consider
  moving them to their own namespace under a restrictive NetworkPolicy so they
  **cannot reach Loki / DB-admin / other infra directly** and must go through the
  backend's RBAC'd endpoints (e.g. `/api/runs/{id}/logs`). The Loki ingress policy
  already admits only `component=backend`, not agent pods — namespace isolation
  makes that boundary structural and shrinks the agent blast radius.

### Stage 3 — In-cluster Forgejo pulls

**Punted (2026-06-04)** — deferred for later; agent images still pull through the
backend registry proxy for now. The rest of this stage stands as the eventual design.

- Point agent `imagePullSecret`s at the in-cluster Forgejo Service; kubelet pulls
  directly.
- Reduce the backend registry proxy to external-CI push only (keep
  `REGISTRY_HTTP_RELATIVEURLS` / `Location` rewriting on that path as needed).
- **Done when:** pod start / grader restart no longer depends on the API server.
- **Risk:** low–medium (URL/pointer rewriting for the pull path).

### Stage 4 — kind-based test harness

- Replace the Docker/testcontainers e2e fixtures with a session-shared **kind**
  cluster fixture; validate kind on the RBE workers (DinD/cgroups) early.
- **Done when:** e2e tests run the real flow against a real k8s API.
- **Rationale:** prerequisite for testing Stages 5–6 (controller-managed
  grader/critic pods) against a real k8s API + RBAC.
- **Spike (2026-06-04): BLOCKED on BuildBuddy RBE — the Firecracker guest kernel
  lacks `CONFIG_KEYS`.** Under the default `workload-isolation-type: firecracker`,
  kind comes most of the way up (privileged containers, cgroup v2, registry egress,
  and `kind load image-archive` of a Bazel `oci_image` all work; kubeadm gets through
  certs + static pods), but the kubelet crash-loops in `setupKernelTunables`:
  `open /proc/sys/kernel/keys/root_maxkeys: no such file or directory` — the microVM
  kernel (5.15) has no kernel-keyring subsystem, so the control plane never comes up.
  - **Switching isolation doesn't help — it swaps blockers.** Under
    `workload-isolation-type: oci` the action runs on the executor _host_ kernel
    (6.16, `/proc/sys/kernel/keys/root_maxkeys` present), but unprivileged
    (`uid=1001`, `CapEff=0`, no Docker — `dockerd` refuses, "needs root privileges").
    Firecracker = root in a stripped-kernel VM; oci = real kernel but no privilege.
    No isolation type offers both.
  - **Paths forward** (kind needs both the keyring _and_ privilege; only firecracker
    provides the privilege half):
    1. Get `CONFIG_KEYS=y` into the Firecracker guest kernel (BuildBuddy-platform
       change). The single missing piece — everything else already works under
       firecracker. Cleanest; no per-k8s-version maintenance.
    2. Custom `kindest/node` with a kubelet patched to drop the `root_maxkeys` /
       `root_maxbytes` tunables — works under firecracker today, but build + maintain
       a node image per k8s version.
    3. Hybrid: `envtest` (apiserver + etcd, no kubelet → no `CONFIG_KEYS`) for the
       `k8s_executor` Pod-spec/reconcile logic + keep Docker/testcontainers for "the
       agent container actually runs." Unblocks real k8s-API testing now but does
       **not** remove Docker.
  - **Until one of these lands, Stage 4 (and the Docker removal it would enable) is
    parked; the Docker/testcontainers harness stays.**

### Stage 5 — Keep grader Pods controller-managed (no Deployments)

**Decision (2026-06-04): graders stay bare `Pod`s created by the controller — the
#1882 reconcile-from-API model — and are _not_ converted to `Deployment`s.**

The security boundary is **_privileged creds never enter any agent pod_**: the host
(controller, holding admin DB creds) mints a fresh per-run Postgres role + `AgentRun`
and injects only that narrow, ephemeral credential into each pod it creates — exactly
the critic model. This works _because the controller creates the pod_. A `Deployment`
hands generation creation to k8s: rollouts and crash-restarts spin up pods from a
fixed template with **no host hook** to provision a per-generation role first. That
leaves only two ways to credential a Deployment-born grader, both worse:

- **Self-registration** — the pod mints its own role/run at startup. Needs standing
  privileged creds _inside_ the pod → erodes the boundary.
- **Bootstrap handshake** — the pod fetches per-run creds from the backend using a
  standing bootstrap identity baked into the template. Adds a long-lived shared
  secret and a new code path for no real gain.

What Deployments would have bought — k8s-owned "one running," restart-on-crash, and
reconcile-from-API robustness — **#1882 already delivers** from the controller (one
labeled grader per snapshot; adopt healthy, reap duplicate/orphan/wrong-image/terminal;
survives backend restarts). The only _net-new_ thing a Deployment adds is
template-driven rollout, not worth weakening the cred posture.

**Consequence — graders keep the per-run ephemeral-cred model (like critics).** The
earlier "stable per-snapshot role `grader_<snapshot>`" idea was a workaround for the
Deployment model's standing-pod problem (a self-registering pod can't mint a role
without admin creds); with the host minting creds per generation, drop it. Each grader
generation gets a host-created `AgentRun` + per-run HMAC role, RLS-scoped by
`current_agent_run_id()` like every other agent. This converges Stages 5 and 6 on one
pattern — _the controller creates the pod and injects ephemeral per-run creds_ —
differing only in lifecycle (graders: one-per-snapshot, respawned; critics: one-shot,
`restartPolicy: Never` + `activeDeadlineSeconds`).

**Remaining hardening (not a conversion):**

- **Run-per-generation.** The controller opens a new `AgentRun` (+ per-run role) each
  time it spawns/replaces a grader, so "restart from fresh context" and "new image"
  each produce a distinct run. It already reaps wrong-image pods (#1882); extend it to
  open the successor's run and finalize the predecessor's.
- **Crash finalization.** Mark a predecessor run terminal on roll/crash — watch-based
  finalizer vs. "next generation finalizes its predecessor" (see Open questions).
- **Current-grader image pointer.** Flux image automation writes the current grader
  digest into a `ConfigMap`; the controller reconciles grader pods onto it (replacing
  the registry-proxy `grader_definition_changed` notify + builtin-tag).
- **Keep, don't retire, `GraderSupervisor`.** It _is_ the controller this stage hardens.
- **Done when:** each grader generation is a host-created run with ephemeral creds, and
  image pushes roll grader pods via image-automation → ConfigMap → controller. (The
  "never orphan/duplicate on backend rollout" goal is already met by #1882.)
- **Benefits from (no longer hard-depends on):** Stage 4 (kind e2e) for testing
  run-per-generation + finalization; #1882 already shipped this model to prod without it.

### Stage 6 — Critics as controller-managed Pods

- The controller creates critic `Pod`s on request from the API (critic_dev calls
  the API to request a run; agents hold no k8s RBAC), with per-run role,
  `restartPolicy: Never`, `activeDeadlineSeconds`.
- A finalizer watches completion → writes terminal `AgentRun` status (exit code
  → `EXITED`/`FAILED`/`TIMED_OUT`) → deletes the pod. No log copy to the DB.
- **Done when:** critics run as native pods; `_collect_run`'s log-to-DB path is
  unused.
- **Depends on:** Stages 2, 4.

### Stage 7 — Remove the bespoke layer

- Delete `DockerExecutor`, the Docker bits of the executor abstraction, and the
  registry's spawn/`_collect_run` log-to-DB code. **Keep `GraderSupervisor`** — the
  reconcile-from-API controller is the irreducible custom piece (Stage 5), not part
  of the bespoke layer being removed.
- Drop the now-dead `container_stdout`/`container_stderr` columns (migration)
  once nothing reads them.
- Tombstone this plan.

## Migration / cutover

- **Graders (Stage 5):** no Deployment cutover — graders stay controller-managed
  pods, and #1882 already did the unlabeled→labeled adoption/reaping in prod. What
  remains is the run-per-generation bookkeeping (the controller opens/finalizes
  runs), transparent to running pods.
- **Roles:** graders keep per-run ephemeral roles — there is no stable
  `grader_<snapshot>` role to create or retire (that idea is dropped with the
  Deployment model).
- Stages 1–3 are transparent to running agents (proxy/registry repointing);
  schedule a window only if `OPENAI_BASE_URL` / pull endpoints change for
  already-running pods (they won't re-read env, so let the next generation pick
  it up).

## Open questions

- **Crash finalization for graders:** self-report on graceful exit is easy;
  hard crash/OOM needs a controller watch to mark the prior run terminal. Decide
  watch-based finalizer vs. "next generation finalizes predecessor."
- **Critic Pods vs Jobs:** revisit `ttlSecondsAfterFinished` if controller-down
  cleanup robustness matters.
- **Budget enforcement under native workloads:** keep enforcement at the proxy
  (deny over-budget calls → agent errors out) rather than killing pods; confirm
  that's sufficient vs. an active reaper for runaway-but-idle pods.
- **Controller home:** fold into the API server vs. its own Deployment. A brief
  controller gap is safe — the reconcile loop is level-triggered (the next
  `snapshot_created`/periodic tick re-derives desired state) and existing grader
  pods keep running meanwhile — so either works.

## Non-goals / what stays

- The eval data model (`reported_issues`, `grading_edges`, fitness), RLS scoping,
  and drift via Postgres `LISTEN/NOTIFY` (already backend-independent) are
  unchanged. This plan swaps the **execution substrate**, not the eval semantics.
