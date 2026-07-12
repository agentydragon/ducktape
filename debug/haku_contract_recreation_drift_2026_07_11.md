# Haku contract-recreation drift audit

Date: 2026-07-11
Repository revision: `2d3755b05cef8891f6890479fabe41507fe19d11` (`devel`), plus the in-progress console screenshot-gallery changes present in the working tree.

## Why this audit exists

The console screenshot gallery independently recreated the tool-call card instead of rendering the production component. When custom per-tool titles and previews evolved, the gallery did not inherit them and silently stopped exercising the behavior it appeared to demonstrate.

This audit looked for the same structural failure throughout `haku/**`:

1. one component owns a contract or state transition;
2. another component manually reconstructs some of it;
3. the reconstruction is not forced to evolve with the owner; and
4. code, tests, fixtures, prompts, or documentation have already diverged, or a planned extension will deterministically expose the gap.

The findings below distinguish confirmed current divergence from latent extension traps. Each finding identifies the owner, the recreation, the consequence, and the narrowest useful consolidation.

## Executive summary

The highest-priority findings are:

- frontend argument schemas already reject valid FastMCP calls and make custom previews fall back to raw JSON;
- the dispatch database and Kubernetes independently model job lifecycle, producing reversible terminal states and orphaned Secrets;
- long-lived runtimes refresh repositories at process startup rather than at the wake boundary;
- console OAuth recreates part of the MCP SDK flow and has already missed a current-protocol `resource` branch;
- OAuth status, refresh, and execution independently derive token state and can contradict or overwrite one another;
- the deterministic credential gate does not recognize Haku's own generated credential formats.

The common remedy is not simply more unit tests. The recreated behavior should be removed, generated from the owner, or covered by a parity/contract test that compares the recreation directly to the owner.

## Confirmed current divergence

### B. Dispatch database state recreates Kubernetes Job lifecycle incompletely

Severity: high

Owner:

- Kubernetes Job deadline and TTL in `cluster/k8s/haku/dispatch/dispatcher/job-template.yaml:18-21`

Recreation:

- `JobStatus` in `haku/dispatch/models.py:9-13`
- result and kill transitions in `haku/dispatch/db.py:103-111`
- result and delete endpoints in `haku/dispatch/app.py:165-190`

Observed divergences:

- deadline expiry, eviction, OOM, pod startup failure, or a failed result POST leaves the database row permanently `created`; there is no Job watcher or reconciler;
- a result arriving after an explicit kill is accepted because result submission checks only whether `row.result` exists, then overwrites `killed` with `completed` or `failed`;
- the same-named per-job Secret has no owner reference (`haku/dispatch/k8s_jobs.py:43-49`);
- normal completion does not delete the Secret or revoke the LiteLLM key; cleanup exists only on explicit kill (`haku/dispatch/k8s_jobs.py:93-102`).

The documented SQL source of truth can therefore lie, terminal states are reversible, and Secrets containing prompts and tokens accumulate after Job TTL cleanup.

Consolidation:

- define one explicit dispatcher-owned terminal-state machine;
- reconcile Kubernetes Job conditions into it;
- reject every terminal-state regression;
- revoke keys and remove companion Secrets on every terminal path; and
- owner-link or reconcile the Secret lifecycle.

Required tests: timeout/no-result, eviction/start failure, normal-completion cleanup, and kill followed by a late result.

### C. Repository freshness is implemented at process startup instead of the wake boundary

Severity: high

Owner:

- wake procedure in `haku/run.md:41-68,122-126`
- live-base promise in `haku/base/README.md:5-12`

Recreation:

- Runtime B clone/pull in `haku/runtime/managed_agent/self_hosted/entrypoint.sh:17-34`, followed by a long-lived poller at `:41-45`
- Runtime C bootstrap in FastAPI lifespan at `haku/runtime/agent/supervisor.py:51-64`, while `wake()` at `:42-48` only calls the already-created agent

The canonical procedure requires fresh operator state, intake, and ducktape base before orientation. Both long-lived runtimes instead synchronize once per process. A scheduled fresh session does not rerun the pod entrypoint or lifespan.

Base changes, UI-written responses, and new intake can remain invisible until the process restarts. The README's statement that the self-hosted worker fast-forwards on every wake is currently false.

Consolidation:

- implement a shared pre-wake synchronization operation used by every runtime; and
- integration-test two wakes while advancing both remotes between them.

Runtime B also depends on a manually advanced ducktape mirror (`cluster/k8s/haku/agent-worker/README.md:16-23`), so refreshing only the local clone does not fully satisfy the promise.

### D. Console OAuth recreates the pinned MCP SDK flow and misses a protocol branch

Severity: high

Owner:

- pinned `mcp==1.26.0` OAuth flow (`requirements_bazel.txt:3334-3344`)

Recreation:

- discovery, dynamic registration, authorization construction, exchange, authentication, and refresh in `haku/console/mcp_operator_oauth.py:372-590`

`_resource_for_oauth` returns `None` when protected-resource metadata is absent. MCP 1.26 instead uses the canonical MCP resource URL for protocol `2025-06-18` and newer even without protected-resource metadata, and includes it in authorization, exchange, and refresh requests. Haku advertises `LATEST_PROTOCOL_VERSION` but follows the older resource-selection branch.

Existing tests provide protected-resource metadata and use only `token_endpoint_auth_method="none"`, so the local implementation and fake server can drift together.

Consolidation:

- delegate to the SDK using durable per-operator storage and callback adapters; or
- reuse the SDK's pure OAuth context/resource decisions.

Add parity tests for protected-resource metadata present/absent, static registration/DCR, `none`/`client_secret_basic`/`client_secret_post`, and refresh.

### E. OAuth status and execution independently define "connected"

Severity: high

Recreation 1:

- status treats any stored association as connected in `haku/console/mcp_operator_oauth.py:184-201,352-363`

Recreation 2:

- execution requires a non-expired access token or usable refresh token in `haku/console/mcp_operator_oauth.py:294-319` and `haku/console/mcp_approval.py:467-478`

An expired, non-refreshable association therefore renders as Connected in settings while reflection is degraded and approval execution returns "Connect your account." `client_secret_expires_at` is persisted but not included in the displayed state.

Consolidation:

- define one shared token-state derivation with at least `unconnected`, `connected`, and `needs_reconnect`; and
- use it for status, metadata/reflection, and execution.

Test expired/no-refresh, expired client secret, and refresh failure.

### F. OAuth refresh and reconnect are competing state writers

Severity: high

Refresh snapshots a locked association, commits, performs network I/O, then reacquires the row and unconditionally writes the result (`haku/console/mcp_operator_oauth.py:294-319`). Reconnect can replace the same association during that gap (`:239-286`).

A refresh from the old client can therefore write its access token into a newly reconnected association. Concurrent refreshes can also both consume a rotating refresh token.

Consolidation:

- serialize refresh per association;
- include the association revision, client ID, and refresh token in an optimistic compare-and-swap; and
- discard stale responses.

Test refresh-versus-refresh and refresh-versus-reconnect interleavings.

### H. Credential lint recreates the credential catalog but misses Haku's own formats

Severity: high

Owner:

- actual generated credentials in `tf/gitops/haku-state/main.tf:29-31,99-101`
- dispatcher credentials in `cluster/k8s/haku/dispatch/dispatcher/credentials.yaml`

Recreation:

- regex catalog in `haku/dispatch/prompt_lint.py:11-22`

The lint calls itself a zero-false-negative deterministic layer, but it recognizes branded prefixes, JWTs, PEM, and age keys only. Haku's Git password, console token, and dispatcher secrets are opaque generated alphanumerics and match none of those patterns.

The LLM classifier might still reject such a prompt, but that does not satisfy the asserted deterministic guarantee.

Consolidation:

- derive test vectors from the real credential generators/formats;
- compare against known credential values where the trust boundary permits; and
- either add a defensible opaque/high-entropy detector or narrow the stated guarantee.

### J. Calendar preview reinterprets the canonical date-time model incorrectly

Severity: medium/high

Owner:

- RFC3339 `EventDateTime` model in `haku/console/tools/google_calendar_client.py:22-41`

Recreation:

- frontend date interpretation in `haku/console/frontend/tool_previews/google_calendar.tsx:30-45,102-129`

The backend accepts instants such as `2026-09-15T16:00:00Z` with `time_zone="America/Los_Angeles"`. The preview slices the wall-clock text and applies the declared zone, displaying 16:00 PDT rather than the correct 09:00 PDT. It also uses the start zone for both endpoints and drops reminder method, so email and popup reminders are indistinguishable.

Consolidation:

- parse the RFC3339 instant and format each endpoint in its own declared zone;
- display reminder method; and
- test offset-to-zone conversion, cross-zone events, and email reminders.

### K. Base source guides still recreate the retired state-owned method

Severity: medium/high

Owner:

- source-guide ownership rules in `haku/base/AGENTS.md:9-15,30-35`

Recreations include:

- Gmail `prepared_prompt` item creation in `haku/base/sources/gmail.md:10-20`;
- Tana `done`/`rejected` reconciliation and `suggestion`/`prepared_prompt`/`body` schema in `haku/base/sources/tana.md:74-80,124-139`; and
- similar method instructions in Drive, Tasks, and Ducktape source guides.

The base contract says source files describe access and interpretation mechanics, not item schema or "look for/surface" procedure. These instructions survived the item-agnostic, state-owned-method migration. Changing the method in state therefore does not remove the older method from Haku's prompt.

Consolidation:

- reduce source docs to channel contracts and access semantics;
- move procedure and artifact schema into state; and
- add a hygiene check for forbidden method vocabulary or generate source docs from structured metadata.

### L. nginx and FastAPI independently implement shell routing and security policy

Severity: medium

Recreations:

- FastAPI headers/cache and fallback in `haku/console/app.py:40-55,107-120,153-167`
- nginx policy and fallback in `haku/console/default.conf.template:1-5,26-38,64-65`

nginx hides backend headers and recreates them. It serves `index.html` for every unknown SPA route, while FastAPI manually registers only `/tool-calls`. Backend tests can therefore pass while production routing or security policy differs, and every new route creates another hidden propagation obligation.

Consolidation:

- use a final non-API FastAPI catch-all if the development fallback remains; and
- generate both header policies from one declarative source or test the built nginx container against representative paths.

### M. Runtime C repeats the known shallow-clone/base-pin failure

Severity: medium/high; Runtime C is currently undeployed

Owner:

- arbitrary prior base pin required by `haku/base/instructions.md:285-293` and `haku/run.md:54-59`

Recreation:

- `HAKU_DUCKTAPE_CLONE_DEPTH=1` default in `haku/runtime/agent/config.py:21-25`
- configured clone in `haku/runtime/agent/bootstrap.py:54-57`

Runtime B's bring-up history already records depth one breaking this exact revision-range operation. Runtime C repeats it. `haku/runtime/agent/test_bootstrap.py:18-34` calls the clone helper without the configured depth, accidentally testing its unrelated full-clone default.

Consolidation:

- use a full clone or deepen/fetch until the stored pin resolves; and
- test through `Settings` and bootstrap with at least two relevant commits.

## Additional confirmed drift and lower-priority seams

### Source and prompt duplication

- `haku/runtime/agent/agent.py:67-76` restates an older miniature `run.md` sequence and omits later base-adoption, response-reduction, approved-result-sweep, and run-manifest requirements. Keep only a pointer to the canonical procedure.
- `haku/runtime/claude_web_env/run.md:86-98` and `bootstrap.sh:93-101` use the presence of `haku-state/items` as a readiness sentinel even though `haku/run.md:35-37` permits Haku to replace that working format. Use repository validity or a method-neutral completion marker.

### Idempotency reconstruction

`haku/dispatch/k8s_jobs.py:23-24` derives identity only from the caller's idempotency key, while the companion Job and Secret depend on prompt, zone, model, and budget. Existing DB rows are returned without payload comparison, and in a race the second request can replace the Secret while accepting the first request's already-created Job. Persist and compare a canonical request fingerprint; reject same-key/different-payload requests and make the Secret immutable once the Job exists.

### Preview adapters copying remote result contracts

`haku/console/tools/grocy.py:45-79` hardcodes remote tool names and result shapes that are independently owned by `grocy_mcp/batch_tools.py`. Tana support similarly hardcodes `read_node` arguments and parses a markdown comment convention. Tests recreate fake servers instead of checking the real same-repository owner. Prefer structured MCP output schemas/shared models and contract tests against the actual implementation or pinned artifact.

## Latent extension traps

These are not currently failing the deployed configuration, but their propagation gaps will deterministically surface when the planned extension lands.

### P. Dispatch zone abstraction carries only namespace and model names

`haku/dispatch/config.py:26-32` models namespace and allowed models. The Job template globally hardcodes `HARNESS=claude` and `ANTHROPIC_*` authentication (`cluster/k8s/haku/dispatch/dispatcher/job-template.yaml:44-66`), while the classifier defines only the ZAI policy. The worker already has a Codex branch, and the planned OAI zone requires it.

Adding OAI to `zones.yaml` can pass current parity tests while launching Claude with the wrong authentication and admission policy.

Make zone configuration discriminated and complete: harness, wire protocol, credential environment, and policy ID. Render/classify/launch one contract case per configured zone.

### Q. Auto-approval and manual approval duplicate the transition into `RUNNING`

Auto-approved submission is persisted as `RUNNING` before execution authentication is acquired (`haku/console/mcp_approval.py:148-185,617-619`). Manual approval acquires authentication before the transition (`:674-679`). If auto-approval expands to an OAuth or static-bearer server and credential acquisition fails, the call remains `RUNNING` forever.

Route both paths through one approve/execute orchestrator and test failed credential lookup.

### R. MCP transport mode is inferred from several independent facts

`haku/console/mcp_config.py:37-53` permits optional `server_url`, bearer, and OAuth fields without a discriminated transport type. `haku/console/app.py:80-105` separately hardcodes in-process registrations, and resolution silently prefers an in-process registration over a configured remote URL.

During an in-process-to-remote migration, adding `server_url` can leave the old in-process server active. Use discriminated `in_process`/`remote` configuration, validate unique IDs, and validate the configuration against the registry at startup.

### S. Managed-agent parity tests normalize away real fields

`haku/base/agent_shared.yaml` claims full toolset identity across cloud and self-hosted surfaces, but `haku/base/test_agent_config_ssot.py:28-40` discards `default_config.enabled`. Self-hosted MCP toolsets explicitly enable it while cloud Terraform omits it. The cloud runtime is parked, so this is dormant. Compare the complete normalized tool/default configuration or generate both surfaces from the shared model.

### T. Bridge runtime validation is manually synchronized with the TypeScript union

`haku/shared/bridge_protocol/protocol.ts` owns the inbound message union, while `haku/console/frontend/bridge.ts` manually parses each discriminant and field from `unknown`. All six current variants are handled, so no present divergence was found, but tests omit geolocation-watch start/stop. A new union branch or field does not force the runtime validator to evolve.

Prefer a shared runtime schema that infers the TypeScript type, or add an exhaustive discriminant-level contract test.

## Verified non-findings

The audit checked and ruled out several tempting false positives:

- `ToolCallRecord.title` is currently propagated through request, ORM, pending projection, and response.
- Drawer and history card structure now genuinely share `ToolCallCard`; no second card skeleton remains after the screenshot fix.
- The legacy launch capability and MCP launch tool both delegate to the same `RoutineLauncher`.
- The Gmail auto-approval tool-name allowlist is an intentionally reviewed security policy, not a catalog that should automatically inherit every new tool.
- The current ZAI zone is internally consistent with the globally hardcoded Claude/Anthropic path; finding P becomes functional when a second zone is added.

## Recommended repair order

1. A — actual FastMCP schema as preview-schema owner.
2. B — reconciled dispatch lifecycle and terminal cleanup.
3. D, E, F — consolidate OAuth protocol and token state.
4. C and M — wake-boundary synchronization and valid history.
5. G and H — repair security/source capability contracts.
6. J — correct Calendar presentation.
7. K and L — remove remaining method and routing shadows.
8. P through T — close the extension traps before enabling the affected features.

## Audit disposition

This was a read-only investigation. No production code was changed as part of the audit. Existing unrelated and in-progress working-tree changes were preserved.
