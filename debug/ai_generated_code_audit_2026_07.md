# AI-Generated Code Audit — ducktape monorepo (2026-07-01)

Audit of the ducktape monorepo against the "AI-Generated Code Audit Framework"
(architectural, async, security, logic, quality, and iterative-regression passes).
The framework targets defect classes empirically over-represented in
heavily-AI-authored codebases; ducktape (~2,700 agent-authored PRs, ~620k
first-party LOC) is squarely in scope.

- **Scope:** first-party code across `x/agent_server`, `agent_core`, `mcp_infra`,
  `airlock`, `grocy_mcp`, `haku`, `props`, `finance` (+ `augur`), `loom`,
  `gmail_archiver`/`gmail_api`, `openai_utils`, `tana`, `wt`, `devinfra`,
  `trilium`, `cluster/`, `tf/`. Excluded: `props/specimens/**` (intentionally
  defective eval fixtures), vendored/generated code, `bazel-*`, `node_modules`,
  lockfiles.
- **Method:** parallel read-only auditors, one per rubric pass, each required to
  cite a real `file:line` and trace the code path before reporting. The async
  pass independently re-verified its sub-agent claims and rejected 3 false
  positives; the highest-severity finding was independently reported by 3 separate
  auditors. Findings below were spot-checked against source.
- **Shallow-clone limitation:** the working clone is ~50 commits deep, so Pass 6.1
  before/after git-history regression analysis was not possible; Pass 6 is
  limited to current-state cross-session boundary analysis.
- **Scope note:** findings already remediated are omitted; this report lists only
  outstanding issues.

## Executive summary

The codebase is, overall, **well above the framework's baseline expectations for
AI-generated code**. The pervasive anti-patterns the rubric predicts are largely
absent: dependency verification found **zero hallucinated packages** across
Python, Rust, and npm; test quality is high (no presence-only or
mock-asserts-the-mock tests in a 20-file sample); STYLE.md's comment-noise ban is
respected; secrets are SOPS-encrypted; SQL goes through SQLAlchemy (no injection
surfaces found); and several services show genuinely complete security controls
(the agent-server policy gateway is server-side enforced, grocy_mcp is properly
OAuth-gated, study_casino uses argon2 + HMAC-audited RNG).

That said, the audit surfaced a **Critical plus a cluster of High** issues
concentrated in exactly the places the framework predicts: **security-critical
branch logic that approximates rather than implements** (the policy engine's
deny-continue path), **silent value-dropping at cross-session module boundaries**
(gmail filter sync, grocy price/location handling), and **statistical/money math
that is subtly wrong** (augur dilution Jacobian, props recall mean-of-means). The
most urgent open items are the policy-engine deny-continue bypass (a denied tool
call is still executed) and scoping down the `kubeapi_admin` ServiceAccount from
`cluster-admin`.

| Severity   | Count | Definition (adapted from framework Part V)                                                                              |
| ---------- | ----- | ----------------------------------------------------------------------------------------------------------------------- |
| Critical   | 1     | Approval-gate bypass reachable from untrusted input                                                                     |
| High       | ~26   | Exploitable secret exposure, over-deletion, wrong money/scoring math, production-path swallowed errors, lifecycle hangs |
| Medium     | ~35   | Incomplete controls, silent value drops, races that self-heal, orphan-state teardown bugs                               |
| Low / Info | ~40   | Dead code, cosmetic abstractions, duplication, stale docs/comments                                                      |

Numbers are approximate; the detailed findings below are the source of truth.

---

## Critical

- **[Critical]** `x/agent_server/mcp/approval_policy/engine.py:517-520` — **Answering
  "deny but continue" on a pending tool call executes the denied call.** The
  `DENY_CONTINUE` branch of `PolicyAdminServer.decide_call` resolves the pending
  future with `ContinueDecision()` — byte-identical to the `APPROVE` branch
  (513-514) — and the gateway ASK path treats any `ContinueDecision` as approval,
  recording `POLICY_ALLOW` and running `call_next(context)` (lines 358-362). The
  inline comment claims "The call is skipped but turn continues," but the code does
  not skip. (The static-policy path at line 345 correctly raises a denial; only the
  human-decision hub path is wrong.) _Verified._
  **Fix:** add a distinct `DenyContinueDecision` variant and map it to
  `_policy_denied_error(ApprovalDecision.DENY_CONTINUE, ...)` in the middleware so
  the call is denied but the turn proceeds.

---

## Pass 3 — Security

- **[High]** `cluster/k8s/agents/airlock/kubeapi-admin-exec-mcp-rbac.yaml:8-14` — the
  ServiceAccount backing the kubectl-exec MCP is bound directly to `cluster-admin`;
  any path that reaches this backend (e.g. an approved airlock tool call) inherits
  full cluster control, so this is an oversized blast radius. **Fix:** scope the
  ClusterRole to the specific verbs/resources the exec MCP needs, or fence behind
  Kyverno dual-control.
- **[High]** `mcp_infra/authentik_auth/auth.py:172` (and airlock fallback
  `airlock/app.py:237`) — **JWTs are verified for signature and issuer but never for
  audience.** No `audience=` is passed to any `JWTVerifier` in the repo, and
  `extra_jwt_issuers` deliberately widens accepted issuers to sibling Authentik
  apps — so a token minted for any other app sharing the signing key is accepted.
  **Fix:** pass each resource's expected `audience`.
- **[High]** `llm/html/llm_html/server.py:45` — token-signing secret defaults to
  `"hunter2"` (`TOKEN_SECRET = os.environ.get("TOKEN_SECRET", "hunter2")`), used to
  sign/verify auth tokens; the README documents the default. **Fix:** raise at
  startup if unset.
- **[High]** `trilium/papers/paper_widget.js:106` (also
  `trilium/issue_tracker/issue_widget.js:151`) — OpenAI API key logged verbatim:
  `console.log("OPENAI API KEY:", OPENAI_API_KEY)`. **Fix:** delete both log lines.
- **[High]** `props/backend/app.py:165-166` — admin token written to pod logs at INFO
  (`logger.info(f"Admin token: {admin_token}")` + admin URL with `?token=`); k8s log
  access is broader than admin-token access. **Fix:** remove or gate behind an
  explicit dev-mode flag.
- **[Medium]** `devinfra/firecracker/manager/service.py:38-43` — an unset
  `FC_MANAGER_AUTH_TOKEN` makes `_require_auth` return immediately, silently
  disabling all auth on the VM-manager API with no log line. **Fix:** fail closed or
  log loudly at startup.
- **[Medium]** `x/agent_server/mcp_bridge/auth.py:131-134` /
  `server/mcp_routing.py:80` — bearer tokens (long-lived static secrets from YAML,
  no expiry/rotation) are compared via plain `dict.get`, not constant-time;
  `mcp_infra/static_bearer.py` correctly uses `hmac.compare_digest`. **Fix:** use
  `secrets.compare_digest` or move to the JWT path.
- **[Medium]** `x/agent_server/server/app.py:62-67` — with `ADGN_UI_CORS_ORIGINS="*"`,
  `CORSMiddleware` is configured `allow_origins=["*"]` **and**
  `allow_credentials=True`. Browsers reject the combination today, but it is a
  footgun if later "fixed" by reflecting Origin. **Fix:** forbid credentials with
  wildcard origins.
- **[Medium]** `cluster/k8s/firecrawl/app/httproute.yaml` + `configmap.yaml` —
  `firecrawl.allegedly.works` is exposed with no Authentik proxy route and
  `USE_DB_AUTHENTICATION: "false"`; an unauthenticated caller can drive
  crawl/scrape (SSRF-ish egress + resource abuse). **Fix:** put behind the Authentik
  outpost or enable Firecrawl auth.
- **[Medium]** `x/gatelet/server/endpoints/admin.py:79` — admin session cookie set
  `httponly=True` but without `secure`/`samesite` (experimental `x/`, not deployed).
  **Fix:** add `secure=True, samesite="lax"`.
- **[Low]** `airlock/config.py:91` — backend `Authorization` header presence is
  checked case-sensitively on a plain dict, so a lowercase `authorization` would be
  overwritten with the shared `EXEC_BACKEND_TOKEN`. **Fix:** normalize header keys.
- **[Low]** Terraform-state Postgres connection strings across ~14
  `cluster/k8s/**/terraform.yaml` use `sslmode=disable`; monitoring S3 backends use
  `insecure: true`. In-cluster traffic only, no embedded credentials. **Fix:** enable
  TLS if the cluster later spans untrusted segments.
- **[Info]** `x/agent_server/policy_eval/runner.py:70` — policy sandbox ships
  `ReadonlyRootfs: False` behind a TODO while `x/agent_server/AGENTS.md` promises a
  read-only rootfs; network isolation (the primary control) is intact. **Fix:** mount
  a tmpfs venv and re-enable, or fix the doc.

**Verified non-issues:** grocy_mcp is properly Authentik-OAuth-gated; the agent-server
policy gateway is genuinely server-side enforced and blocks reserved-code spoofing;
`tana`'s `DEFAULT_FIREBASE_API_KEY` is a Firebase Web key (designed public); all
`*.sops.yaml` carry real `ENC[...]` + `sops:` metadata; non-SOPS `kind: Secret`
matches are all ExternalSecret `remoteRef` templates; the only `shell=True` sites are
operator-controlled dev tooling, not network-reachable; no SQL injection surface
(SQLAlchemy throughout); haku's forward-auth header is fenced by a NetworkPolicy.

---

## Pass 1 — Architectural integrity

### Dead / orphan modules (several already flagged in the repo's own `docs/dead_code_2026_01_30.md`)

- **[Medium]** `agent_core/progress.py:16` — `OneLineProgressHandler` has zero
  importers/BUILD deps; already listed in `docs/dead_code_2026_01_30.md` 5 months
  ago. **Fix:** delete the module + `py_library`.
- **[Medium]** `agent_core/logging_utils.py:16` — `configure_logging` is a divergent
  near-copy of `util/logging.py`'s, its only consumer being
  `x/agent_server/logging_config.py`; both retain a stale `MINICODEX_DEBUG` env
  switch. **Fix:** point the consumer at `util.logging` and delete.
- **[Medium]** `mcp_infra/client_helpers.py:7` and
  `mcp_infra/authentik_auth/store.py:15` — both modules have zero importers and zero
  Bazel reverse-deps. **Fix:** delete.
- **[Medium]** devinfra Python-ported-to-Rust leftovers — `claude/shell.py`,
  `claude/streaming.py`, `claude/supervisor/service_utils.py`,
  `claude/env_file.py:95` (`write_env_file`, ported to `env_file.rs`),
  `claude/claude_api/hooks/dispatch_input.py:38` (`AnyHookInput`, reimplemented in
  `protocol.rs`), `firecracker/manager/process_api_client.py` (+ its sole dependent
  `protocol.py`): all zero live consumers. **Fix:** delete the modules and their
  `py_library` targets, or tombstone with a verifiable gate.
- **[Medium]** `tana/export/{sample,cli}.py`,
  `tana/export/headless_autoexport/record_from_profile.py` — three dead modules
  also flagged in `docs/dead_code_2026_01_30.md`; `sample.py` executes at import
  with a hardcoded personal path. **Fix:** delete + BUILD targets.
- **[Medium]** `tana/render/inline_refs.py` — `replace_inline_refs`,
  `process_inline_refs`, `find_inline_date_refs` and duplicated regexes have zero
  callers; the two render modules duplicate each other. **Fix:** keep the one live
  symbol (`parse_inline_date`), delete the rest.
- **[Medium]** `props/orchestration/agent_registry.py:791` — `AgentRegistry.get`,
  `list_recent`, and their `AgentRunView`/`from_orm` product have zero callers.
  **Fix:** delete.
- **[Medium]** `wt/server/wt_server.py:152` + `wt/server/repo_status.py` — `RepoStatus`
  is constructed and never read; `summarize_status` has zero callers (ahead/behind
  is computed via the GitRefsWatcher path). **Fix:** delete, or wire in if its
  worktree-HEAD semantics were the intended ones.
- **[Medium]** `x/agent_server/server/exceptions.py:10` — `AgentNotFoundError`,
  `AgentSessionNotReadyError`, `PolicyOperationError` are defined but never
  raised/handled. **Fix:** wire the promised FastAPI handlers or delete.
- **[Medium]** `openai_utils/openai_api_examples/stateless_two_step_demo.py:1`,
  `loom/gym/analyze_baselines.py`, `devinfra/js/debundle/live_proxy/browser_runner.py`
  — orphan modules with no Bazel target/consumer; unbuilt and unlinted. **Fix:** add a
  `py_binary` (matching each file's own docstring) or delete.
- **[Medium]** augur dead schema/sampler modules (each verified zero importers
  repo-wide): `finance/augur/api/accounting.py:24` (5 wire models),
  `finance/augur/model/poisson_events.py:24` (`sample_events` + single-member
  `ScalarEventSpec` union), `finance/augur/model/path_models/_density.py:10`
  (`gaussian_logpdf*`), plus write-only fields `calibration/calibration.py:147`
  (`resolution_criterion`), and the duplicated/dead tax helpers in
  `sim/runtime.py:32` (`estimated_tax_quarter` duplicated verbatim in
  `compiler/obligations.py:67`; the rest of runtime.py's tax surface unreferenced).
  A further cluster of unused exports (`fixed_point.py:64` `quanta_to_quantity`,
  `round_float_array_to_int64`; `model/conditioning.py:55` `iter_observations`; etc.)
  and a second RNG-substream convention (`trained_private_equity.py:187` `seed ^
0x5EED` vs the canonical `derive_stream_rollout_seeds`). **Fix:** delete the dead
  modules/fields/helpers; unify the RNG derivation.
- **[Low]** `devinfra/claude/claude_api/profile.py:43`,
  `mcp_infra/compositor/server.py:259` (`extract_tool_*schemas` pure-delegation
  wrappers), `agent_core/handler.py:109` (`SequenceHandler`), `airlock/storage.py`
  `update_state`/`get_log_entries_since` — dead exports whose only references are
  their own tests or an error-message menu string. **Fix:** delete.

### Orphan state / write-only fields

- **[Medium]** `x/agent_server/server/app.py:147` — shutdown does `if
container._ui_manager: ...` but `AgentContainer` defines no `_ui_manager` (live
  attrs are `self.ui`/`self._cm`); a guaranteed-dead teardown path against a
  nonexistent attribute. **Fix:** delete the legacy branch or fix to `self.ui`.
- **[Medium]** `mcp_infra/compositor/resources_server.py:100` — `SubscriptionRecord.pinned`
  is never set `True`, making several branches unreachable and an exported field
  constant. **Fix:** remove the field + pinned branches, or implement pin semantics.
- **[Low]** `agent_core/agent.py:353` (`_function_call_map`, written never read),
  `agent_core/events.py:98` (`Response.created_at`/`idempotency_key`, never
  assigned), `tana/render/html_utils.py:25` (`_suppress_next_leading_space`),
  `wt/server/gitstatusd_listener.py:433` (`_count_limits`),
  `grocy_mcp/tool_metadata.py:27` (`resource`/`tags` never set) — write-only fields
  against STYLE.md "every field needs a reader." **Fix:** delete.

### Dead branches / phantom guards / cosmetic abstractions

- **[Medium]** `agent_core/agent.py:636` — the entire tool-call abort machinery is
  unreachable: `ToolCallOutcome.was_aborted` is never set `True` by the only invoker,
  so the parallel cancel-scope path and both abort cascades can never execute.
  **Fix:** delete `was_aborted`/`abort_triggered` and both branches.
- **[Medium]** `mcp_infra/exec/seatbelt.py:190` — a literal `if False:` block feeds a
  write-only `unified_sandbox_denies_text` field (always `None`), plus a blanket
  `except Exception: raise ToolError` that deviates from the documented
  no-blanket-except convention. **Fix:** delete the dead block/field; narrow the catch.
- **[Low]** `agent_core/agent.py:775` — phantom `should_sample_llm` branch:
  `decision` is provably `NoAction` by this point, so the `else: raise TypeError` is
  unreachable. **Fix:** inline the sampling block.
- **[Low]** `haku/state_template/ui/backend/reads.py:75` — `yaml.safe_load(...) or {}`
  can never produce a valid `RunManifest` (required `run_id`), it only degrades the
  error message; violates "no defaulting to empty on parse errors." **Fix:** drop `or {}`.
- **[Low]** `airlock/oauth/provider.py:77` — single-subclass `_BaseProvider` with a
  `self`-less method (cosmetic abstraction) plus `refresh_token=data.get(..., "")`
  empty-string absence sentinel. **Fix:** inline; model absence as `str | None`.
- **[Info]** `x/agent_server/persist/types.py:58` — `Persistence` Protocol has one
  implementation and no test fake; call sites annotate the concrete
  `SQLitePersistence`, so it provides no isolation seam. **Fix:** use the Protocol at
  boundaries consistently (gain a fake) or drop it.

### Monolithism (context-induced)

- **[Info]** `tana/litellm_proxy/provider.py` (1270 lines) — **genuine** context-induced
  monolith mixing ≥5 concerns: k8s-secret subprocess fetching, Firebase token
  refresh (with sync/async near-duplicates that also duplicate
  `firebase_resigner/resigner.py`), an HTTP client, a large OpenAI↔Tana message
  mapper, and a multi-format stream parser. **Fix:** split into `credentials.py`,
  `client.py`, `messages.py`, `streaming.py`, thin `provider.py`.
- **[Info]** `props/backend/routes/runs.py` (1102 lines) — **moderate** monolithism: an
  in-memory job subsystem and ~80 lines of LCB/Pareto selection analytics embedded
  inline among the routes. **Fix:** move the job system to `orchestration/`, the
  selection logic to a query/stats module.
- **[Info]** `grocy_mcp/batch_tools.py` (1494 lines), `props/db/models.py` (1590),
  `finance/augur/sim/engine/jax_engine.py` (3098),
  `finance/augur/model/private_equity_risk.py` (1253) — large but **domain-cohesive**;
  the grocy file's one ~1330-line registration closure is where the Pass-2/Pass-4
  per-section convention drift lives, so a per-domain split would also fix those.

---

## Pass 2 — Asynchronous logic & state

- **[High]** `airlock/proxy_server.py:373` — every action's approval pipeline is a
  fire-and-forget task whose exception is never retrieved; a crash in
  `_update_and_notify` leaves the action stuck PENDING forever with the agent
  polling a dead action. **Fix:** add a done-callback that logs and transitions the
  action to a terminal error state.
- **[High]** `airlock/proxy_server.py:248` — `client.list_tools()` in
  `_try_connect_backend` sits outside the try/except; a post-connect failure escapes
  and, raised inside the unguarded `_reconnect_degraded_backends` loop (270-276),
  permanently kills reconnection — degraded backends never recover. **Fix:** move
  enumeration inside the guarded region; wrap the reconnect loop body.
- **[High]** `airlock/proxy_server.py:201` — graceful shutdown hangs forever: the
  shielded cleanup `await asyncio.gather(*self._background_tasks, ...)` awaits an
  infinite `_reconnect_degraded_backends` loop and parked `_await_human_decision`
  tasks that are never cancelled, so backend-client/`storage.close()` cleanup never
  runs (tests mask it via `force_exit=True`). **Fix:** cancel background tasks before
  gathering (as `oauth_facade/server.py` and `app.py` already do).
- **[High]** `airlock/frontend/api.ts:171-177` (+42-48) — `getApiClient()` caches
  `connect()`'s promise; the initial `fetch("/api/events")` has no try/catch, so one
  rejection (server restarting on SPA load) poisons `_clientPromise` permanently —
  every later `await getApiClient()` rejects until a full reload. Line 75's SSE
  reconnect chain also dies permanently if a reconnect fetch rejects (floating
  promise). **Fix:** catch connect failures, retry with backoff, never cache a
  rejected promise; `.catch`-reschedule the reconnect.
- **[Medium]** `props/orchestration/grader_supervisor.py:225-231` — `reconcile()` is
  not serialized: `_run_debounced_reconcile` clears `_debounce_task` before awaiting
  `reconcile`, so a trigger during an in-flight reconcile spawns a second concurrent
  one; two overlapping reconciles can double-spawn graders and the unguarded
  `self._handles = new_handles` clobbers freshly-spawned collectors. **Fix:** an
  `asyncio.Lock` around `reconcile()`.
- **[Medium]** `x/agent_server/server/runtime.py:168-170` — `_run_impl` catches all
  agent-run exceptions and only logs them (comment: "Error now logged, not sent via
  dead send_payload"); the HTTP client and UI observe a run that silently ends.
  **Fix:** persist/broadcast a terminal run-failed event.
- **[Medium]** Fire-and-forget tasks whose exceptions are never retrieved (only
  `_bg_tasks.discard` done-callback), so failures surface only via asyncio's GC-time
  fallback (or not at all when a strong ref is held):
  `x/agent_server/server/runtime.py:71-79` (UI event delivery),
  `x/agent_server/mcp/approval_policy/engine.py:614,643,697,702` (pending-approval
  broadcasts — a dropped one means the operator silently misses a notification),
  `props/backend/cli.py:47` (`grader-initial-spawn`),
  `props/orchestration/grader_supervisor.py:210-213` (debounce task). **Fix:** a shared
  `_spawn` helper with a logging done-callback (the correct pattern already exists in
  `persist/handler.py` and `props/backend/app.py:178-186`).
- **[Medium]** `x/agent_server/persist/handler.py:30-34` — the done-callback calls
  `task.exception()` unconditionally; on a cancelled persistence task this raises
  `CancelledError` inside the callback. `drain()` also re-raises a summary
  `RuntimeError` naming only exception _types_, discarding tracebacks. **Fix:** guard
  `if task.cancelled(): return`; chain original exceptions.
- **[Medium]** Svelte lifecycle leaks on the singleton client:
  `airlock/frontend/App.svelte:77-113` (route `$effect` cleanup races its own async
  IIFE — `unsubscribe` assigned only after an `await`, so route changes leak the
  subscription and refire floating `loadList()`),
  `airlock/frontend/BackendStatus.svelte:19-21` (discards the unsubscribe fn; each
  visit adds another callback), `airlock/frontend/api.ts:88-90` (`getAction().then`
  with no rejection handler). **Fix:** check a `cancelled` flag after each await;
  keep and register unsubscribe fns in `onDestroy`; `.catch` refetches.
- **[Low]** `gmail_archiver/event_classifier.py:183-185` — `extract_batch` gathers
  without `return_exceptions`; one failed extraction aborts the batch while siblings
  keep running unawaited (wasted API spend). **Fix:** `return_exceptions=True` with
  per-item error accounting (match the grocy_mcp per-item pattern).
- **[Low]** `x/agent_server/server/app.py:131-133` — shutdown wraps `stack.aclose()`
  in `contextlib.suppress(Exception)` while the comment claims errors "will be logged
  by the caller"; `suppress` guarantees they are not. **Fix:** `logger.exception`.
- **[Info]** `x/agent_server/mcp/approval_policy/engine.py:620-624` — leftover
  `logger.warning("[EVAL_START] ...")` step-tracing on every policy evaluation in the
  production path (repo style forbids committed ad hoc instrumentation). **Fix:** remove.

_The async auditor verified and **rejected** 3 sub-agent claims as false positives
(a GNOME "signal leak," a subprocess-callback-after-destroy guarded by `_destroyed`,
and a "never-cancelled SSE" on an app-lifetime singleton) — noted here as a
confidence signal._

---

## Pass 4 — Logic & business-rule integrity

### Money / statistical math (finance + augur)

- **[High]** `finance/augur/fit/bayes_dilution.py:348` — Jacobian error: the fit
  stores `annual_dilution_rate_log_sigma` as the SD of `log(1+r)`
  (`np.std(np.log1p(rate))`), but the sampler
  (`model/private_equity_risk.py:1171`) defines that knob as the sigma of `log(r)`.
  The sibling OLS fit (`dilution_prior.py:147-166`) does the required
  `(1+r)/r` delta-method conversion; the Bayesian path does not. At `r≈0.27` the
  per-rollout dilution dispersion is understated ~4.7×, biasing every mark-path
  quantile from this prior. **Fix:** return `np.std(np.log(rate))` (rate>0) or apply
  the `(1+r)/r` factor.
- **[Medium]** `finance/augur/model/private_equity_risk.py:1069-1070` — primary-round
  jump math is inconsistent for `step_up ≠ 1`: the valuation marks up injected cash
  by the step-up factor, so post-round per-share value spuriously exceeds the round
  price (`s=1.2, c=0.08` ⇒ +1.25%/round, compounding). Masked today because callers
  pin `step_up_median=1.0`, but the knob is config-reachable. **Fix:** `log_v +=
log(step_up + cash_over)`; fix `plans/mint_streams_model.md:127` too.
- **[Medium]** `finance/augur/budget/sql_read_model.py:337` — the stale-override probe
  uses `removed IS FALSE` only, weaker than the classification CTE
  (`pending IS FALSE AND link.status != 'revoked'`), so an override on a revoked-link
  transaction silently no-ops without being reported — the exact relink scenario the
  feature was built for. **Fix:** use the same liveness predicate.
- **[Low]** `finance/augur/model/state_space.py:405-419` — `ObservationTreatment`
  semantics collapse: `INFORMATIVE` observations are skipped entirely and `NOISY_MARK`
  is treated as `HARD_START` (exact overwrite), discarding the mark's `log_sigma`;
  the persisted `filtered_log_state_cov` is never read. **Fix:** implement the filtered
  update, or delete the unused plumbing and document hard-start-only behavior.
- **[Low]** `finance/augur/budget/sql_read_model.py:291` — lumpy detection filters on
  signed amount + expense-only, contradicting the `abs(amount)` config contract.
- **[Low]** `finance/plaid/db/schema.py:80` — transaction/balance/holding money is
  stored as SQL `Float` end-to-end and aggregated in-DB (`sum(...)::float`),
  accumulating binary-float error in money sums (Beancount export is protected by
  per-tx `Decimal.quantize`). **Fix:** `Numeric(scale=2)`.
- **[High]** `finance/augur/sim/engine/jax_engine.py:3022` — property-sale market value
  is anchored to the home-value series level at **month 0** instead of the property's
  **purchase month** (`_scale_money(purchase_price, series_row[:, month] /
series_row[:, 0])`), so a mid-horizon purchase sells at a price including
  appreciation from before ownership; the portfolio-valuation path in the same file
  (`product_metrics`, 1266-1274) correctly divides by `levels[:, :, purchase_month]`.
  Gross proceeds, realized gain, §1250 recapture, §121 exclusion, LTCG, and net cash
  are all wrong by `level[purchase_month]/level[0]`; all sale tests purchase at month 0,
  masking it. **Fix:** base on `series_row[:, ev_purchase_month]`.
- **[Medium]** `finance/augur/sim/engine/jax_engine.py:2634` (+1820-1835) — a tax year
  whose accrued tax is ≤ the safe harbor is never settled (`true_up = max(actual −
safe_harbor, 0)` is 0, so `TAX_TRUE_UP` never fires and the liability stays
  outstanding forever, contradicting `REQUIREMENTS.md:633-637`); estimated-tax
  overpayments (`actual < 0.75·prior`) are never refunded, permanently losing cash.
  **Fix:** emit a zero-amount-tolerant settlement and refund or document the leak.
- **[Medium]** `finance/augur/sim/codec/assets.py:150,156` &
  `compiler/private_equity.py:139-144` — PE disposition decode passes cash-slot
  **indices** where string-table **codes** are required, so `source/proceeds_account_id`
  decode to the wrong strings (e.g. `"alice"` instead of `"checking"`); relatedly
  `PrivateEquityTenderPolicy.proceeds_account_id` is silently ignored (`del
proceeds_account_code # unused`) and proceeds always land in the owner's first cash
  account, despite `scenario.py:596` promising otherwise. **Fix:** use real string codes
  / resolve the slot via `account_slot_by_key`.
- **[Low]** `finance/augur/sim/engine/jax_engine.py:2309-2313` — `_amount_values`
  computes `reset_month` with no lower clamp, so liquidity/PE-floor schedules with
  `base_month_index > 0` can produce a negative index that JAX wraps to an
  end-of-horizon (future) series level; these schedules are unvalidated at simulate
  time. **Fix:** clamp `reset_month = max(base_month, ...)` or extend validation.

### Destructive Plaid/reconcile paths (finance)

- **[High]** `finance/plaid/db/sync.py:87` — `sync_all` aborts all remaining links on
  the first per-link failure (list comprehension, `sync_link` re-raises), so one
  expired bank login (a documented recurring state, ordered first by
  `institution_name`) starves every other account's daily sync until manually
  repaired. The sibling scraper isolates per-source failures for exactly this reason.
  **Fix:** catch per-link, log, continue, fail the job at the end.
- **[Medium]** `finance/plaid/link/app.py:334-340` — `remove_link` runs a non-atomic
  Plaid→k8s-Secret→DB teardown; any midway failure permanently wedges the link
  (retry 404s on the missing Secret / already-removed Item → 502 before
  `purge_link_data`). **Fix:** treat "already removed"/"already absent" as success;
  purge DB last but unconditionally.
- **[Medium]** `finance/reconcile/cli.py:181,190` — `start_date` is read
  unconditionally but assigned only inside an `if`, so a mapping without it raises
  `NameError` or reuses the previous mapping's value; a memo-matched split whose
  expense was deleted/out-of-window crashes with a raw `KeyError`. **Fix:** default
  `start_date = date.min` per iteration; use `.get()` with an explicit error path.
- **[Low]** `finance/reconcile/cli.py:113` — `assert amount < 0` crashes the import
  for any Splitwise expense where the user's net is positive (a valid lending case),
  even though the split construction below is sign-symmetric. **Fix:** drop the assert.

### gmail_archiver (destructive Gmail ops)

_Guard trace: there is no `messages.delete`/`batchDelete` anywhere — bulk ops are
reversible `batchModify` label changes and `autoclean-inbox` defaults to dry-run.
The genuinely destructive surfaces are filter deletion, label deletion, and local
`.eml` deletion — all three have defects._

- **[High]** `gmail_archiver/filter_sync.py:114` — `normalize_yaml_rule` silently
  drops criteria it can't represent (`CompoundCondition` for from/to/subject → None;
  `bcc,cc,list,is,category,larger,smaller,...` never read), so `filters sync` creates
  a filter with **broader** criteria than the YAML — including for `trash`-action
  rules, over-deleting mail matching the residual criteria. **Fix:** raise on any
  unrepresentable criteria field.
- **[High]** `gmail_archiver/cli/filters.py:49` — `load_yaml_filters` keeps only
  `isinstance(rule, FilterRule)`, silently discarding `ForEachRule`, and no
  `for_each` expansion exists — so `sync` classifies every filter created from a
  `for_each` rule as extraneous and **deletes it**. **Fix:** expand `ForEachRule` or
  hard-error on presence.
- **[Medium]** `gmail_archiver/cli/filters.py:258` — `sync` deletes then creates; a
  per-item create failure is caught, only printed, and exit stays 0 — a modified
  filter (delete+create) whose create fails is permanently lost with no rollback.
  **Fix:** create first, delete only after all creates succeed; propagate to exit code.
- **[Medium]** `gmail_archiver/filter_sync.py:23` — `_strip_gmail_quotes` corrupts
  compound criteria (`"a" OR "b"` → `a" OR "b`); since the YAML side is never
  quote-normalized, quoted-phrase filters diff as changed every run, making `sync`
  perpetually delete+recreate them. **Fix:** strip only single-token quotes; normalize
  both sides identically.
- **[Medium]** `gmail_archiver/main.py:185` — `download-matching` orphan detection
  compares local `.eml` stems against an id set truncated by `--max-results`, so
  emails outside the window are offered for local deletion. **Fix:** skip orphan
  detection when `max_results` is set.
- **[Medium]** `gmail_archiver/cli/labels.py:94` — `labels prune` computes "used by a
  filter" from `add_label_ids` only, while `labels list` also counts
  `remove_label_ids`; a label used only in `removeLabelIds` is shown as used yet
  deleted. **Fix:** include `remove_label_ids`.
- **[Medium]** `gmail_archiver/planners/aliexpress.py:195` — naive/aware `TypeError`
  crashes the whole `autoclean-inbox` run (`compute_deadline` returns naive datetimes,
  compared against `datetime.now(UTC)`); the terminal `delivered`/`closed` states the
  docstring promises to archive are never handled. **Fix:** return UTC-aware deadlines;
  add the terminal-state branch.

### grocy_mcp (stock math / unit conversions)

- **[High]** `grocy_mcp/batch_tools.py:541` — mutating stock POSTs (`/add`,
  `/consume`, `/inventory`) run inside `_retry`, which retries on
  `httpx.TimeoutException`: a POST Grocy applied but whose response timed out is
  re-sent, double-applying the mutation — the same stock-inflation class as the
  module's own documented 2026-04-17 incident. **Fix:** retry mutating POSTs on
  429/5xx only, or verify via stock-log before re-posting.
- **[High]** `grocy_mcp/batch_tools.py:398` — `stock_get`'s `locations` filter and
  `location_name` use the product's **default** location (`/stock` rows carry no
  per-location amounts), while the docstring tells agents to use this tool to choose
  the `location` for `stock_consume`; stock at a non-default location is reported
  under the wrong one, steering consumes to the wrong place. **Fix:** source
  per-location amounts from `GET /stock/locations`, or drop the claim.
- **[Medium]** `grocy_mcp/batch_tools.py:513` — unit conversion is applied to `amount`
  but not `price`, so when `qu != stock_qu` the recorded per-stock-unit price is wrong
  by the conversion factor (price per crate stored as price per bottle). **Fix:** divide
  price by `conversion_factor`, or document price as per-stock-unit.
- **[Medium]** `grocy_mcp/batch_tools.py:1413` — `get_expiring_stock(days_ahead)`
  reads `/stock/volatile` without `due_soon_days`, so results are pre-truncated to
  the server's ~5-day window; `days_ahead=30` silently returns only ~5 days. **Fix:**
  pass `params={"due_soon_days": days_ahead}`.
- **[Medium]** `grocy_mcp/batch_tools.py:371,655,717...` — retry/semaphore/fail-fast
  conventions drift by section: `raise_for_status()` sits _outside_ the retried
  closure in four read paths (making retry a no-op for 5xx/429); ~half the mutating
  tools bypass the documented semaphore; `stock_entries_list` resolves outside its
  per-item `try`, aborting the whole batch. **Fix:** move `raise_for_status` inside
  each closure; route mutations through `sem`; move resolve inside `try`.

### props (grading / scoring)

- **[High]** `props/db/migrations/versions/20260224000000_materialize_examples.py:223`
  — `recall_by_definition_split_kind` is a mean-of-means over a pooled denominator:
  it averages per-example mean credits, then divides by `SUM(recall_denominator)`, so
  two examples (denom 5, mean credit 2.5) yield 0.25 instead of pooled 0.5 — recall
  deflated ≈`n_examples`, so definitions evaluated on more examples rank
  systematically worse in the leaderboard and best-definition selection. **Fix:** pool
  as `SUM(per-example mean credit)/SUM(denominator)`.
- **[High]** same file `:200` — the `example_counts` CTE's `SELECT DISTINCT` omits
  `snapshot_slug`; all `whole_snapshot` examples have `files_hash = NULL`, so two
  snapshots with equal `recall_denominator` collapse into one row, undercounting
  `n_examples` and inflating recall (interacting with the previous finding). **Fix:**
  include `snapshot_slug`.
- **[Medium]** `props/db/migrations/versions/20251228000000_complete_schema.py:1559` —
  an RLS helper compares `target_metric = 'whole_repo'` (underscore) but the enum is
  `"whole-repo"` (hyphen; the sibling guard at 621 is correct), so the whole-repo
  branch is dead and whole-repo optimizers fall into the train/valid branch, able to
  list VALID-split examples — weakening the black-box validation design. **Fix:** fix
  the literal; add an RLS test.
- **[Medium]** `props/backend/routes/stats.py:426` — coverage-heatmap TP counts use
  `count(distinct occurrence_id)`, but occurrence ids are unique only within a TP, so
  `occ-0` from different TPs collapse. **Fix:** count distinct `(tp_id, occurrence_id)`.
- **[Low]** `props/db/migrations/.../complete_schema.py:591` — `check_edge_credit_sum`
  does read-then-check in a plain trigger; under READ COMMITTED two concurrent inserts
  can jointly exceed the 1.0 credit cap. **Fix:** per-occurrence advisory/row lock.

### props orchestration (deployment/logic)

- **[High]** `props/backend/app.py:141` — the documented compose deployment can't
  boot: startup hard-requires `config.llm_proxy_url`, but the checked-in compose
  config never sets it and has no llm-proxy service, so the backend `os._exit(1)`s in
  a loop; the `config.py:124` comment still claims a removed `backend_url` fallback.
  **Fix:** add `llm_proxy_url` + an llm-proxy service to compose (or restore the
  fallback) and fix the comment.
- **[Medium]** `props/orchestration/docker_executor.py:161` — `PodInfo.image` means
  different things per executor: Docker reports the image **ID**, which can never
  equal the OCI ref compared in `grader_supervisor.py:268`, so on Docker every
  reconcile reaps healthy graders as `wrong_image` and respawns (~every 120s, burning
  tokens); no e2e test exercises Docker+supervisor. **Fix:** tag/label containers with
  the OCI ref so `PodInfo.image` is runtime-consistent.
- **[Medium]** `props/backend/routes/runs.py:368` — module-level `_jobs` dict grows
  unboundedly (never evicted, pins each job's `asyncio.Task` + examples) and is lost
  on restart, contradicting the package's explicit no-global-state/reconcile-from-DB
  pattern. **Fix:** evict terminal jobs or persist as rows; drop the global dict.
- **[Medium]** `props/orchestration/agent_registry.py:684,453-457` —
  `run_critic_dev_improve`'s `output_dir` param is dead (leaks an empty tempdir per
  call); budget enforcement checks only the immediate parent's remaining budget,
  ignoring running children and ancestor limits (documented, unfixed → recursive
  LLM-spend over-allocation). **Fix:** delete the param; implement the two budget checks.
- **[Low]** `props/agents/runtime.py:73`, `props/llm_proxy/app.py:43`
  (+`registry_proxy/app.py:38`), `props/core/oci_utils.py:55` — a `Database` threaded
  through template rendering but never used; self-created `Database`s never
  `dispose()`d in lifespan (unlike `backend/app.py:199`); `pull_authority` ignores
  `pull_port` unless `pull_host` is also set. **Fix:** drop the unused param; dispose;
  compute host/port independently.

### loom (scoring)

- **[High]** `loom/gym/inspect_harness.py:296` — `headline_metric` has no categorical
  branch: for `kind == "categorical"` it returns `"mean_pinball"`, but
  `_score_categorical` produces only `{log_loss, brier[, rps]}`, so the lookup at
  line 334 (outside the `try`) raises `KeyError` and crashes the scorer for **every**
  categorical task. **Fix:** categorical → `"log_loss"`; add a categorical scorer test.
- **[Medium]** `loom/gym/series_tasks.py:125` — ceiling/floor ground truth is resolved
  from partially-observed windows (`max/min_observed_between` skip missing months and
  require only one observation), so an interior data gap can mint a wrong
  `BinaryOutcome` for a threshold the missing month crossed; `path_tasks.py` correctly
  skips holey windows. **Fix:** require full window coverage unless the observed
  extremum already decides the outcome.

### x/agent_server policy (beyond the Critical)

- **[High]** `x/agent_server/runtime/container.py:327` — active policy is never
  rehydrated from persistence (`get_latest_policy` has zero production callers), so
  admin `set_policy` and approved proposals silently revert to default on agent
  restart. **Fix:** hydrate from `persistence.get_latest_policy(agent_id)` and persist
  in `set_policy`.
- **[Medium]** `x/agent_server/policies/default_policy.py:19` — default policy
  auto-ALLOWs any tool whose mount prefix begins `resources_`, because
  `parse_tool_name` splits at the first `_`, so `resources_backup_wipe` parses as
  prefix `resources`. **Fix:** match against the actual mount list or forbid `_` in
  mount prefixes.
- **[Medium]** `x/agent_server/mcp/approval_policy/engine.py:167,360` — `await_decision`
  has no cancellation cleanup (a cancelled run leaves a ghost `pending` entry
  forever; unknown `call_id` still returns `SimpleOk`); human ASK decisions are
  audit-logged as `POLICY_ALLOW`/`POLICY_DENY_ABORT`, so the trail can't distinguish
  human approval from policy auto-allow (the `USER_*` outcomes are never written).
  **Fix:** `try/finally` cleanup + surface unknown ids as errors; record `USER_*`
  outcomes on hub-resolved paths.

### Other

- **[Low]** `haku/state_template/ui/backend/forgejo.py:109` — `create_file` treats any
  HTTP 422 as idempotent success, so a second distinct feedback in the same UTC
  second collides on `intake/{stamp}-feedback.md` and is silently dropped while the
  API returns `ok`. **Fix:** inspect the 422 body; add a uniquifying suffix.
- **[Medium]** `tana/litellm_proxy/provider.py:270` — rotated Firebase refresh tokens
  are discarded (`FreshTokens.refresh_token` populated, never read), so on rotation
  the client re-uses the stale token (and re-execs `kubectl`) until it invalidates;
  the sibling `firebase_resigner/resigner.py:206` handles rotation. **Fix:** store the
  rotated token back or drop the field.

**cpap** traced clean (atomic tmp+rename downloads, git ops outside the card-WiFi
window). props **division-by-zero on empty sets is handled by design**
(`scale_stats`/`StatsWithCI.scaled` return zero-stats on a zero divisor; CI is
NULLed for n<2).

---

## Pass 5 — Code quality & maintainability

### Logging hygiene (in addition to the Pass-3 High secret-logging items)

- **[Medium]** `gmail_archiver/gmail_client.py:183,224,311-322,...` — library code emits
  ad-hoc `print(..., file=sys.stderr)` (incl. full rate-limit response bodies) instead
  of `logging`. **Fix:** module `logger`, body dumps at DEBUG.
- **[Medium]** `llm/html/llm_html/server.py:303` — logs a 20-char auth-token prefix on
  every successful verification. **Fix:** log a hash or nothing.
- **[Low]** `finance/worthy/worthy/graph.js:7-8,38`,
  `trilium/issue_tracker/hotlist_table.js:86` — leftover debug `console.log` in
  production JS. **Fix:** remove.

### Env/config validation (silent fallbacks masking misconfig — beyond the Pass-3 items)

- **[Medium]** `props/llm_proxy/routes.py:118` — upstream API key silently defaults to
  `""` deep in the request path (confusing per-request 401 instead of startup
  failure). **Fix:** validate configured upstreams' key env vars at startup.
- **[Low]** `x/agent_server/mcp_bridge/auth.py:60-62` — missing `tokens.yaml` degrades
  to empty token config with only a warning (fail-closed, but masks a deploy
  misconfig as a mysterious all-401). **Fix:** fatal outside dev mode.
- **[Low]** `x/gitea/pr_gate/policy_common.py:8-14` — `GITEA_ADMIN_TOKEN` defaults to
  `""` and a `PRQ_PER_REPO` JSON parse error is swallowed to `{}` (a typo silently
  reverts repo limits). **Fix:** let the parse error propagate; require the token.

### Duplication (cross-session lost-context artifacts)

- **[Medium]** `agent_core/logging_utils.py` ↔ `util/logging.py` — ~45 lines of
  logging bootstrap duplicated (identical `dictConfig`/structlog, both with a stale
  `MINICODEX_DEBUG` knob). **Fix:** keep `util/logging.py`, delete the copy.
- **[Medium]** `props/agents/runtime.py:100-115` ↔
  `x/editor_agent/agent_pkg/output.py:69-85` — Mako template-context builder
  duplicated cross-package and already diverging. **Fix:** extract a shared module.
- **[Medium]** Three divergent "transient HTTP error" retry predicates
  (`grocy_mcp/batch_tools.py:152`, `finance/scraper/http_fetch.py:35`,
  `openai_utils/retry.py:20`) with materially different semantics (one omits
  `ConnectError`, one retries `501`); `gmail_archiver/gmail_client.py:265-355`
  hand-rolls a 90-line blocking retry loop while the repo standard is tenacity (10
  packages), and its README TODO still claims retry doesn't exist. **Fix:** a shared
  `util/` httpx-transient predicate; port gmail to tenacity.
- **[Medium]** `_run_alembic_migrations` reimplemented 5× across sibling packages
  (`airlock/storage.py:52`, `finance/plaid/db/link_store.py:41`,
  `x/study_casino/store.py:74`, `x/gatelet/server/lifespan.py:138`,
  `finance/augur/budget/test_sql_read_model.py:54`) with drifting signatures. **Fix:**
  one `util/` helper taking `(conn, migrations_dir)`.
- **[Low]** Two `run_command`/`_run_command` functions with opposite failure
  contracts (`devinfra/ci/bb_runner_probe.py:111` never raises vs
  `x/editor_agent/agent_pkg/output.py:27` raises vs `haku/runtime/agent/agent.py:43`
  shell+truncate); three `sha256_file` variants (raise / return None / return "")
  across `devinfra/ci` + `web_env`; duplicated bench scripts and augur prior
  constants. **Fix:** rename to reflect contract; consolidate one typed
  subprocess-wrapper + one hashing helper per package.

### Cross-boundary datetime hygiene (Pass 6.1/6.3)

- **[High]** `airlock/storage.py:70-71` — `created_at`/`updated_at` default to naive
  server-local `datetime.now` in a tz-naive column; the frontend
  (`ActionList.svelte:6`) reinterprets the offset-less ISO as browser-local, so
  displayed action times are wrong by the server/browser offset (pods run UTC). The
  same package's OAuth side already uses `datetime.now(UTC)` — two conventions split
  along dev-session lines; the frontend test harness even uses a `Z`-suffixed format
  the backend never produces. **Fix:** store aware UTC; align the harness fixtures.
- **[Medium]** `wt/shared/protocol.py:273,277` + `wt/server/types.py:24` — the wt
  JSON-RPC status response mixes naive-local timestamps with the aware-UTC commit
  dates from `git_manager.py:127`; the two sibling `_now()` helpers
  (`wt/server/types.py` naive vs `x/agent_server/persist/sqlite.py:24` aware) have
  opposite semantics. **Fix:** standardize `wt.shared.protocol` on aware UTC.
- **[Medium]** Repo-wide census: 53 bare `datetime.now()` sites vs 101 tz-aware,
  plus a `datetime.utcnow()` in `finance/plaid/db/schema.py`. **Fix:** lint-ban
  `datetime.utcnow()` and bare `datetime.now()`; audit the naive sites at
  DB/serialization boundaries.

### High-complexity functions (worst offenders; accidental unless noted)

`grocy_mcp/batch_tools.py:162` `register_batch_tools` (1333 lines / 125 branches — the
closure idiom scaled past its limit), `finance/augur/sim/engine/jax_engine.py:1137`
`_program_impl` (1152 lines; 3 of the repo's 4 longest functions live in this file),
`mcp_infra/compositor/resources_server.py:319` `__init__` (424 lines, embeds
instructions + all tool defs), `git_commit_ai/git_ro/server.py:163` `__init__` (259
lines, highest branch density). **Fix:** extract tool bodies / decompose by phase;
move instruction strings to constants.

### Test quality

Sampled ~20 test files across packages: quality is **high** — genuinely
behavior-verifying (respx-mocked boundaries, real-Postgres integration, hand-rolled
fakes asserting batch chunking and `ExceptionGroup` semantics, hand-computed expected
literals). **Zero** circular tests and **zero** mock-asserts-the-mock. The only smell
is a little snapshot-everything/assert-doesn't-crash in low-stakes display/smoke tests
(mostly sanctioned by the syrupy convention). Minor items:
`gmail_archiver/test_gmail_link.py:14-22` (test rebuilds the implementation's f-string
then adds redundant substring asserts), `grocy_mcp/test_server.py:12` (presence-only
smoke test asserting nothing), a few statusline/display render tests that assert only
`== snapshot`. **Fix:** add one key-content assertion alongside the snapshot; assert an
expected literal in the link test.

### TODO/tombstone inventory

Census: **208 TODO, 0 FIXME/HACK/XXX** in first-party py/rs/ts/svelte; 29 `CLEANUP(...)`
tombstones, almost all well-formed with verifiable gates. Risk-guarding TODOs worth
promoting to tracked work:

- **[High]** `mcp_infra/exec/bwrap.py:96` (+ `seatbelt.py:215`) — `read_image` does
  `Path(input.path).read_bytes()` on the host with `# TODO: should respect bwrap
sandbox boundaries`; a sandboxed agent can exfiltrate any host-readable file via the
  image tool. **Fix:** resolve paths inside the sandbox root, or drop the tool until
  fixed.
- **[Medium]** `mcp_infra/seatbelt/compile.py:16` — SBPL escaping coverage
  (backslashes/quotes/non-ASCII) is unverified → potential sandbox-policy injection
  via crafted paths; TODO with no round-trip tests. **Fix:** add the tests.
- **[Medium]** `props/core/gepa/warm_start.py:76-84` — feature "temporarily disabled
  during scope_hash migration" unconditionally returns `None` with no gate/tombstone,
  so GEPA silently always cold-starts. **Fix:** finish the migration or convert to a
  gated `CLEANUP(...)`.
- **[Low]** `props/db/models.py:1538` (`started_at` documented never-populated —
  write-only column), `x/claude_linter_v2/llm_analyzer.py:111` (core LLM call is a
  stub under a real-looking API), `skills/freecad/conftest.py:17` (tombstone with a
  non-verifiable gate; guarded constant already commented out). **Fix:** populate/drop;
  tombstone or implement; delete the dead lines.

---

## Pass 0 & 6 — AI markers, dependencies, regression

- **Dependencies (3.6): clean.** Zero hallucinated packages across ~120 Python deps,
  the Rust `Cargo.toml`, and all 5 first-party `package.json` files — a strong signal
  against AI-invented imports. Locked security-relevant pins are current
  (`cryptography==46.0.5`, `urllib3==2.6.3`, `pyjwt[crypto]==2.11.0`, `fastapi==0.135.1`).
  - **[Medium]** `pyproject.toml:99` — ships both `psycopg` v3 and legacy
    `psycopg2-binary` (props/db imports psycopg2, others use v3). **Fix:** consolidate on
    v3.
  - **[Low]** unmaintained deps: `passlib` (last release 2020; used for real password
    hashing → migrate to `argon2-cffi` directly), `atomicwrites` (archived 2022),
    `pytimeparse` (2018), Rust `serde_yaml` (upstream-tagged `+deprecated`). **Fix:**
    migrate when convenient.
- **AI markers (0.2): low density.** STYLE.md's comment-noise ban is largely respected;
  no `# === Section ===` banners, no mid-file camelCase/snake_case switches in sampled
  files. Dependency-pin comments are genuinely informative. The one filename artifact
  is `x/claude_linter_v2/` with no coexisting v1 (naming leftover, not a live
  duplicate). A handful of trivial one-line docstrings restate the function name.
- **Iteration depth (0.3):** not analyzable — shallow clone. The heavy agent-authorship
  ratio (~2,700 PRs) is exactly the profile the framework flags for feedback-loop
  regression risk, which is why the security-control-completeness findings (the two
  Criticals, JWT audience, deny-continue) matter most.
- **Security-control completeness (6.2):** the agent-server policy gateway is a
  notably **complete** control (server-side enforced, blocks reserved-code spoofing) —
  the single most positive security finding. The gaps are the deny-continue variant
  (Critical), missing audience (High), and the non-constant-time token compare
  (Medium), all documented above.

---

## Prioritized remediation

1. **Immediate (block/hotfix):**
   - Fix the policy-engine deny-continue path so a denied call is not executed
     (Critical).
   - Scope down the `kubeapi_admin` cluster-admin binding (High).
2. **This week (security):** JWT audience validation; remove the `hunter2` secret
   fallback (require at startup); delete the OpenAI-key and
   admin-token log lines; fail-closed on unset `FC_MANAGER_AUTH_TOKEN`; fix the
   `read_image` sandbox-boundary escape or disable the tool.
3. **This week (correctness):** gmail filter-sync criteria-dropping + ForEachRule
   deletion (over-deletion risk); grocy retry double-apply on mutating POSTs;
   props recall mean-of-means + missing `snapshot_slug`; loom categorical
   `KeyError` crash; augur dilution Jacobian; plaid `sync_all` abort-on-first-failure;
   props compose `llm_proxy_url` boot failure.
4. **This sprint (async/lifecycle):** airlock fire-and-forget approval tasks, reconnect
   loop, shutdown hang, and frontend SSE promise-poisoning; policy rehydration on
   restart; the fire-and-forget-task done-callback pattern (adopt one `_spawn` helper).
5. **Maintenance:** delete the dead modules (many already in
   `docs/dead_code_2026_01_30.md`); consolidate the duplicated retry/migration/
   subprocess/logging helpers; standardize datetimes on aware UTC; migrate money
   columns to `Numeric`; split the two genuine monoliths
   (`tana/litellm_proxy/provider.py`, `props/backend/routes/runs.py`).

## Toolchain integration (adapted to ducktape's existing CI)

The repo already runs ruff + mypy aspects, buildifier, pre-commit, and a custom
Rust `claude_hook` linter. Highest-leverage additions, mapped to the passes:

| Tool / rule                                                                                                                                                                        | Pass | What it would catch here                                                           |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---- | ---------------------------------------------------------------------------------- |
| ruff `ASYNC`/`RUF006` (unstored `create_task`) + a custom rule                                                                                                                     | 2    | The fire-and-forget-task done-callback gaps                                        |
| ruff `DTZ` (flake8-datetimez)                                                                                                                                                      | 4/6  | Bare `datetime.now()` / `utcnow()` at boundaries                                   |
| A vulture / custom orphan-module gate in CI                                                                                                                                        | 1    | The dead-module cluster (keep `docs/dead_code_*.md` enforced, not just documented) |
| Gitleaks / TruffleHog pre-commit                                                                                                                                                   | 3    | Committed key/token logging + hardcoded secret defaults                            |
| A "no secret default" lint (`os.environ.get(x, <secret-literal>)`)                                                                                                                 | 3/5  | `hunter2`, `FC_MANAGER_AUTH_TOKEN`                                                 |
| Semgrep custom rules: unauth FastAPI routes on an app that also mounts an auth'd sub-app; `allow_origins=["*"]` + `allow_credentials=True`; `JWTVerifier(...)` without `audience=` | 3    | The airlock auth gap, the CORS footgun, the missing-audience class                 |
| A duplication gate (jscpd/`pylint --disable=all --enable=duplicate-code`) at a loose threshold                                                                                     | 5    | The retry/migration/subprocess/logging duplicates                                  |

The framework's SonarQube/CodeQL suggestions are heavier than this repo needs; the
ruff-rule + targeted-Semgrep additions cover the same passes at far lower cost and fit
the existing aspect-based CI.

---

## Methodology & honest calibration

This audit ran the framework's six passes as parallel read-only auditors, each
required to cite and trace a real `file:line`. Confidence is highest on the security,
async, and money-math findings (spot-checked against source; the deny-continue
Critical was re-verified directly). The rubric is a heuristic tuned
for typical AI slop, and several of its predicted anti-patterns **did not hold** here —
which is itself a result worth recording: dependency hygiene, test quality, secret
management, and comment discipline are all strong. The real risk profile is not "AI
slop everywhere" but a smaller set of **incomplete security controls and
silent-value-drop boundaries** — precisely the failure mode the framework's Part VI
("do not trust appearance of correctness") is designed to surface, and the reason the
Critical findings sit behind code that looks correct and passes its tests.
