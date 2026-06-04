# Plan: k8s-native agent execution

**Status:** planning (not yet implementing). Supersedes the bespoke
container-orchestration layer (`props/orchestration/`) with native Kubernetes
workloads.

## Why

The orchestration layer reimplements a Kubernetes controller — in-memory
desired/actual reconciliation, manual restart-on-image-change, manual
kill/cleanup — but without the parts that make controllers robust
(reconcile-from-API, `ownerReferences`/GC, rollout semantics). The tax has been
real and recurring:

- Graders held actual-state in memory, so every backend rollout started a fresh
  generation and orphaned the previous one into duplicate pods per snapshot
  (no `ownerReferences`, no reaping). #1882 added a reconcile-from-API
  controller to patch this; native workloads make the whole class moot.
- Agents are coupled to the dashboard backend: every LLM call is proxied through
  it, so rolling the API server can disrupt in-flight agents.

The fix is to let Kubernetes own pod lifecycle and split the agent **data plane**
(the LLM proxy + DB) from the dashboard **control/read plane** (API + frontend),
so the disposable part can roll freely.

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
  the snapshot set ↔ grader `Deployment`s, and creates critic `Pod`s on request.
  Holds the only cluster-write RBAC; agent pods get none.

**Workloads:**

- **Critics → bare `Pod`s** (`restartPolicy: Never`, `activeDeadlineSeconds` for
  timeout). The controller + DB `AgentRun` are the source of truth; a `Job`
  wrapper would only add `ttlSecondsAfterFinished` (cleanup if the controller is
  down) — adopt later if wanted. No auto-retry (each run is one eval data point
  and costs LLM spend).
- **Graders → `Deployment`s** (`replicas: 1` per snapshot). k8s owns "one
  running" + restart-on-crash (with backoff) + rolling image updates. **Graders
  still have runs:** a _run is a pod generation_ — the grader self-registers a
  new `AgentRun` (`agent_run_id`) at startup, so "restart from fresh context" =
  `rollout restart` and "new image" = template bump, each producing a new run.
  Graders use a **stable per-snapshot role** (`grader_<snapshot>`), not per-run
  HMAC roles, so a pod doesn't need admin creds to mint a role at startup.

## Logs & transcript

Two distinct things, both readable by critic_dev for the agents it launches:

- **Transcript** (LLM turns / tool calls / findings) — captured at the **LLM
  proxy** (it already logs per-request rows keyed by `agent_run_id`; extend to
  the full request/response), stored structured in the DB, RLS-scoped. Survives
  pod deletion and crashes.
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
`ConfigMap` (or directly into the grader Deployment template); Flux rolls the
grader Deployments. This replaces the registry-proxy `pg_notify`
(`grader_definition_changed`) + builtin-tag mechanism entirely.

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

### Stage 2 — Agent-readable logs (transcript + Loki)

- Promtail config: promote `adgn.agent_run_id` to a Loki stream label.
- Add `GET /api/runs/{id}/transcript` and `GET /api/runs/{id}/logs` (the latter
  proxies a Loki query), authorized by run lineage.
- Add the data-plane component(s) to the Loki `NetworkPolicy` ingress allowlist.
- Update critic_dev agent docs/tools to read both.
- **Done when:** a critic_dev agent can fetch both the transcript and raw logs
  of a critic it launched.
- **Depends on:** Stage 1 (transcript capture).

### Stage 3 — In-cluster Forgejo pulls

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
- **Rationale:** prerequisite for testing Stages 5–6 against native workloads.

### Stage 5 — Graders as Deployments

- Controller reconciles the snapshot set ↔ grader `Deployment`s (create on
  `snapshot_created`, delete on removal). This is the only grader controller
  logic left — k8s owns pod lifecycle.
- Stable per-snapshot role `grader_<snapshot>`; update RLS so that role owns its
  runs + grading edges for its snapshot.
- Grader pod self-registers an `AgentRun` per generation; finalize the prior
  generation's run on roll/crash (controller watch or self-report — see Open
  questions).
- Wire Flux image automation → ConfigMap/template for the current grader image.
- Retire `GraderSupervisor` (the #1882 reconcile loop).
- **Done when:** backend rollouts never orphan/duplicate graders; image pushes
  roll grader Deployments via Flux.
- **Depends on:** Stage 4.

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

- Delete `DockerExecutor`, the Docker bits of the executor abstraction, the
  registry's spawn/`_collect_run` log-to-DB code, and `GraderSupervisor`.
- Drop the now-dead `container_stdout`/`container_stderr` columns (migration)
  once nothing reads them.
- Tombstone this plan.

## Migration / cutover

- **Graders (Stage 5):** the moment graders become Deployments, the existing
  bespoke grader pods are orphans the new system won't select — exactly the
  unlabeled→labeled migration we are handling for #1882. Cut over by deleting the
  old generation once the Deployments are up (the new system never recreates
  them).
- **Per-run → stable roles:** create the `grader_<snapshot>` roles before
  retiring per-run grader roles; drop the old roles after cutover.
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
- **Transcript schema:** one row per LLM turn vs. a single blob per run; how the
  dashboard + critic_dev consume it.
- **Controller home:** fold into the API server vs. its own Deployment. A brief
  controller gap is safe (Deployments self-heal), so either works.

## Non-goals / what stays

- The eval data model (`reported_issues`, `grading_edges`, fitness), RLS scoping,
  and drift via Postgres `LISTEN/NOTIFY` (already backend-independent) are
  unchanged. This plan swaps the **execution substrate**, not the eval semantics.
