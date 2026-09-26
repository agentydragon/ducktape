# cdk8s builder/binding deduplication opportunities

Review baseline: `origin/devel` at `ee6ab6ced6` (2026-09-26). A source audit of how
ducktape's `cluster/cdk8s/**` modules call `providers/` wrappers and `cdk8s_plus_34`'s
fluent/raw builder tiers, looking for repeated idioms with no shared helper. Every
citation below was read at the given line, not just grepped.

## A. `flux_kustomization()`'s own defaults are miscalibrated against actual usage

Across all 188 `flux_kustomization(...)` call sites, keyword overrides cluster hard
around a few values the helper doesn't default:

- `timeout` (current default: `None`, Flux's own default) — `"5m"` at 91 sites (48%),
  `"10m"` at 45, `"2m"` at 21, long tail beyond that.
- `wait` (current default: `True`) — `wait=None` at 49 sites, `wait=True` at 10,
  `wait=False` at 5.

`AGENTS.md` states the design stance explicitly: "A default is policy, not the common
value... a per-app choice (`timeout`, `decryption`, `health_checks`) stays on the
node." That's a deliberate, documented decision — but `timeout="5m"` isn't just
common, it's a near-majority. Defaulting it would delete ~90 duplicate keyword args
with zero behavior change for those callers. Flagging as "reconsider the documented
stance," not something to change without confirming intent first.

## B. Gaps in the `providers/` wrapper layer

1. **`gateway.https_route()` can't express what 8 callers need**, forcing raw
   `HttpRoute`/`HttpRouteSpec` construction: `authentik/app.py:229`,
   `authentik/proxy_routes.py:128`, `activitywatch/app.py:233`,
   `study_casino/app.py:336`, `website/website.py:220`,
   `monitoring/grafana_instance.py:194`, `cli_proxy_api/cli_proxy_api.py:258`,
   `matrix/matrix.py:262`. Concrete root causes: `RouteMatch` has no `path_prefix`
   (only `path_exact`); `https_route(hostname: str, ...)` takes one hostname
   (`website.py` needs two); the only filter knob is a boolean `hsts` (three callers
   need different header rewrites). Proposed: add `RouteMatch.path_prefix(...)`, widen
   `hostname` to `hostnames: Sequence[str]`, accept
   `extra_filters: Sequence[RouteFilter] = ()`.

2. **No `ClusterExternalSecret` wrapper** alongside the existing `ExternalSecret` one —
   3 near-identical raw builds: `alloy_otlp_bearer.py:42-64`, `haku/mailbox.py:482-505`,
   `airlock.py:159-179`, differing only in `namespaces`/store/target names. Mirror
   `ExternalSecret`'s shape in `providers/external_secrets/external_secret.py`.

3. **`SandboxTemplate.network_policy_management=UNMANAGED` is passed at 100% of its 4
   call sites** (`haku/workspaces.py:167`, `agent_workspaces.py:90`,
   `agentplane/command_sandbox.py:145`, `agentplane/app.py:476`), each with a
   near-identical justifying comment, even though the wrapper's own documented default
   is `None` → `Managed`. Zero callers want `Managed`. Make `UNMANAGED` the wrapper's
   default.

4. **`Certificate`'s CA-signing-key shape is hand-repeated 4×**:
   `private_key=CertificateSpecPrivateKey(algorithm=ECDSA, size=256, ...)` at
   `haku/kube_api_proxy.py:83`, `cert_manager/interception_ca.py:80`,
   `github_api_proxy/proxy.py:204,228`, paired with the same
   `duration="87600h", renew_before="8760h"` (10y/1y) literal at 3 of those sites.
   Proposed: a `CertificatePrivateKey.ecdsa_p256(rotation_policy=None)` factory plus a
   `LONG_LIVED_CA` constant beside `Certificate`.

5. **One ServiceMonitor auth call site bypasses the wrapper's own factory**:
   `providers/prometheus_operator/service_monitor.py` already has
   `Endpoint.bearer_token_secret(...)`, used correctly by `forgejo/app.py:466` and
   `litellm/proxy.py:396` — but `home_assistant/app.py:549` reaches past it for the raw
   `authorization=ServiceMonitorSpecEndpointsAuthorization(...)` field directly. Check
   whether the wrapper's factory needs an `authorization=`-based variant, or whether
   this site should just switch to the existing one.

## C. Repeated ducktape-specific idioms with no shared helper

1. **"Mint our own bearer secret"** (`Password` generator → single-key `ExternalSecret`
   template) — **11 call sites**, ~15-20 lines each: `google_mcp.py:91`,
   `ha_mcp.py:98`, `ssh_mcp/backend.py:68`, `forgejo/app.py:113`, `airlock.py:135`,
   `agentplane/staging.py:429`, `agentplane_index/workers.py:80`,
   `haku_openclaw_spike_config.py:469`, `public_coder_agent_config.py:824`
   (near-verbatim copy of the previous one, same comment), `ollama/app.py:273`,
   `home_assistant/app.py:494`. 9 of these share the exact literal
   `PasswordSpec(length=48, digits=12, symbols=0, no_upper=False, allow_repeat=True)`.
   `agentplane/dex.py:65` already has a local, file-scoped partial version of this
   idea. A `mint_bearer_secret(scope, id, *, name, namespace, key="password", ...)`
   collapses ~180 lines to 11.

2. **Sibling pattern: "mint DB role credential"** — same skeleton, multi-field
   template — 3 sites: `agentplane/database.py:46`, `haku/database.py:106`,
   `plaid_mcp/db.py:91`, all using the identical `PasswordSpec(length=40, digits=8,
...)` literal. Proposed `mint_db_role_secret(...)`, sharing the `Password`-generator
   half with #1.

3. **CNPG `initdb(database=X, owner=X)`** same-name idiom at **13 call sites**:
   `nix_cache/attic.py`, `atuin/server.py`, `langfuse/app.py`, `plaid_mcp/db.py`,
   `haku/mailbox.py`, `haku/database.py`, `litellm/database.py`, `matrix/matrix.py`,
   `study_casino/app.py`, `ntfy.py`, `agentplane/database.py`,
   `agentplane_index/workers.py`, `gatus/app.py`. Low complexity per site; a one-line
   `cnpg.same_owner_initdb(name)` still cleans up 13 places.

4. **"Open web egress" idiom** (`cilium.dns_egress(protocols=["ANY"], resolves=["*"])`
   immediately followed by `EgressRule.to_entities(WORLD, ...)`) at 5 sites:
   `public_coder_proxy.py:421`, `github_api_proxy/proxy.py:406`,
   `agentplane/egress.py:595`, `gatus/app.py:175`, `public_coder_sshpiper.py:276`. Same
   shape as `cilium.py`'s existing `fqdn_fence`/`dns_egress` combinators, just missing
   the "fully open" variant. Proposed:
   `cilium.open_internet_egress(*, ports, entities=(WORLD, REMOTE_NODE, HOST))`.

5. **CNPG "app secret → 5 env vars + connection string"** repeated 4×:
   `agentplane/app.py:212`, `agentplane/electric.py:63`, `agentplane/actions.py:162`,
   `haku/console.py:107` (already locally factored as `database_env()`, but not
   shared). Proposed: `cnpg_connection_env(secret, *, prefix, url_env_name, scheme)`.

6. **Single-port `Service(selector=deployment, ports=[...])`** repeated at ~11 of 15
   total `selector=deployment` sites, near-verbatim. Proposed:
   `simple_http_service(scope, id, *, metadata, deployment, port, name="http")`.

## D. `cdk8s_plus_34` fluent-builder consistency

1. **"Hardened single-container Deployment/Job" bundle** copy-pasted at ~9 sites
   (`pod_metadata` + `select=False`/`.select()` + `automount_service_account_token=False`
   - `enable_service_links=False` + fixed-UID `PodSecurityContextProps`): `ntfy.py:226`,
     `google_mcp.py:128`, `ssh_mcp/backend.py:136`, `aiquota.py:243`,
     `haku/kube_api_proxy.py:150`, `haku/migration.py:53`, `haku/console.py:340,432`,
     `clickhouse/schema.py:43`.

2. **`apply_pod_spec_patches` helper is half-adopted**: 8 sites use it, 9 reinvent
   `ApiObject.of(x).add_json_patch(runtime_default_seccomp_patch())` inline instead
   (`ssh_mcp/backend.py:150`, `aiquota.py:267`, `ha_mcp.py:224`, `ntfy.py:278`,
   `litellm/proxy.py:329`, `haku/kube_api_proxy.py:216`, `haku/migration.py:86`,
   `haku/console.py:404,474,551`) — partly because the helper's signature is typed
   `Deployment`-only and can't accept the `Job`s among those 9. Widening to `Workload`
   would let them adopt it.

3. **Zero fluent `CronJob(`/`DaemonSet(`/`StatefulSet(` construction exists anywhere**
   — every CronJob (9 sites) and DaemonSet (4 sites) goes through raw
   `k8s.KubeCronJob`/`k8s.KubeDaemonSet` instead, each hand-rebuilding the same
   container-security/resource values tier-1 callers get for free.
   `pod_spec_patches.py:15`'s `CRON_JOB_POD_SPEC_PATH` constant is dead code as a
   result (zero references, confirmed).

4. **`capabilities=drop([ALL])` has no shared constant in either tier**: 10 tier-1
   sites (`ContainerSecutiryContextCapabilities`), 21 tier-2 sites
   (`k8s.Capabilities`). `agentplane/container_security.py`'s `WRITABLE_ROOT` is the
   closest thing but bundles in `read_only_root_filesystem=False`, so it doesn't fit
   callers wanting drop-ALL with the default filesystem.

5. **Redundant `allow_privilege_escalation=False`** on 6 tier-1 call sites (including
   inside `WRITABLE_ROOT` itself) — `AGENTS.md` already documents that cdk8s-plus
   always emits this regardless of props, so it's a no-op everywhere it appears.

6. **16 files independently define a local `_secret_env(name, secret, key)` helper**
   instead of one shared one (two flavors needed: raw `k8s.EnvVar` vs. `EnvValue`).

7. **`agentplane/container_security.py` and `agentplane/node_scheduling.py` are already
   cross-package shared helpers in practice** — `haku/console.py`, `haku/database.py`,
   `haku/migration.py` all import them despite the modules' own docstrings calling them
   Agentplane-specific. Per `AGENTS.md`'s own convention, promote both to package root.

8. **`"reloader.stakater.com/auto": "true"`** hand-typed in 43 files with no shared
   constant — a typo here silently disables config-reload-triggered restarts.

## Housekeeping (cheap, no design decision needed)

- 6 of 34 `image-pins/kustomization.yaml` files repeat a drifted-wording explanatory
  comment (`google-mcp`, `ssh-mcp`, `haku/console`, `haku/mailbox`,
  `haku/workspaces/app`, `agent-sandbox/workspaces`) that `AGENTS.md` already says not
  to repeat per-directory. Delete them.
- 8 call sites use `depends_on=[flux_kustomization_depends_on(x)]` where
  `flux_kustomization_depends_on_many(x)` (the actual shared helper) is equally valid —
  purely cosmetic.
- `SOPS_DECRYPTION` reuse is clean — zero hand-rolled duplicates found anywhere outside
  its one definition in `flux.py`. No action.

## Suggested landing order

Given the repo's split-aggressively-into-PRs convention, most of B and C are
independently landable one wrapper or one helper at a time. **B.3**
(`SandboxTemplate` default) and **C.1** (`mint_bearer_secret`) are the best starting
points: B.3 is a one-line change with the highest caller-count-to-risk ratio, and C.1
has the largest LOC win of anything here.
