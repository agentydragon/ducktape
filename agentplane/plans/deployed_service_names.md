# Deployed service names

**Names-only execution released on 2026-11-17 at 00:03 UTC.** The operator accepted
`sessions` / `environments`, matching image names and bootstrap prefixes. Keep all current
SPIFFE IDs: skip the security-expansion clauses of S1/S2 and all of S6/S7. The relay
rename R0–R4 is unreleased. YAML keys, database names, Lease ID and SSA managers stay.
S0's [rollback record](../debug/2026_11_17_deployed_names_s0.md) captures available live
facts and uncaptured gaps; the coordinator accepts those gaps for S1–S5 and certifies
the last-caller census before S8. S0's digests are the supported rollback images; retain
old Deployments as scaled-to-zero manifests and pins until S8 retirement.

Each stage waits for the previous stage's merge and the coordinator's confirmation that
staging is reconciled, pinned and healthy. Shared Flux object changes require explicit
operator approval. Never restart or force-resume live or suspended work for this migration.
S1 is split into bootstrap readers plus image publication, then image automation. Adding
the automation roster also adds references to shared Receiver `forgejo-images/receiver`;
that edit is held for operator approval. Neither part changes the GitRepository or Flux
Kustomization objects.

The operator released `RUNNER_GATEWAY_RENAME` planning on 2026-11-16 at 20:06 UTC. This plan owns that migration;
[the DAG](task_dag.md#runner_gateway_rename--deployed-names-for-the-sessions-and-environments-binaries)
owns dispatch. The separable [relay section](#egress_relay_rename--separate-approval) covers the
operator's 2026-10-16 `EGRESS_RELAY_RENAME` request. Either section can be accepted independently.

## Proposed names and boundaries

Follow the [Journal and Thread ADR](../docs/adr_journal_and_thread.md) and
[the two-binary decision](task_dag.md#17-two-binaries-and-the-go-layout): an unqualified Session is
an Agentplane Session, never a native harness session or an execution session. This migration
creates no Journal/History service rename, new authority, extra binary or Environments-to-Sessions
call. Source directories remain decided.

| Current deployed name                 | Proposed deployed name    | Scope and rationale                                                                                                                                                                                                    |
| ------------------------------------- | ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `runner-gateway`                      | `sessions`                | `agentplane/session_dispatcher/`: admission, durable Dispatcher queue, routing and automatic wake. No Gateway remains. New public Service `sessions:8080`; retire both old Services only after their callers are gone. |
| `sandbox-controller`                  | `environments`            | `agentplane/environments/`: reconciliation, EnvironmentService, Runtime proxy and Executor. New public Service `environments:8082`; preserve metrics on `8080` and health on `8081`.                                   |
| `agentplane-runner-gateway` image     | `agentplane-sessions`     | Published image name follows the serving binary's responsibility.                                                                                                                                                      |
| `agentplane-sandbox-controller` image | `agentplane-environments` | Same boundary as the Environments Deployment.                                                                                                                                                                          |

Propose matching Deployment/container, ServiceAccount, Role/RoleBinding, PDB, ConfigMap and
telemetry names. Keep Flux owners and database names unchanged. Propose bootstrap env prefixes
`AGENTPLANE_SESSIONS_` and `AGENTPLANE_ENVIRONMENTS_`, including `CONFIG_FILE` and Sessions'
`DISPATCHER_DATABASE_URL`; these are service-process inputs, distinct from managed CLI
`AGENTPLANE_SESSIONS_URL` / `AGENTPLANE_ENVIRONMENTS_URL`, which already have those names.

Security names are a separate acceptance choice: recommend server SVIDs
`spiffe://agentplane/sessions` (currently `spiffe://agentplane/session-dispatcher`) and
`spiffe://agentplane/environments` (currently `spiffe://agentplane/sandbox-controller`). Keep
Sessions' existing outbound `spiffe://agentplane/sessions` and the dedicated
`spiffe://agentplane/runtime-proxy` role. A shared URI does not merge certificates or method grants.
Changing the controller's outbound observer URI is separately gated by step S7; recommend retaining
its old URI until that gate can be proved without interrupting user work. Accepting deployment names
does not require accepting SVID changes. Do not infer any authority from a new resource name.

Proposed concrete artifacts are images `agentplane-sessions` / `agentplane-environments`, binary
entrypoints `/usr/local/bin/sessions` / `/usr/local/bin/environments`, bootstrap
`AGENTPLANE_SESSIONS_CONFIG_FILE` / `AGENTPLANE_ENVIRONMENTS_CONFIG_FILE` and
`AGENTPLANE_SESSIONS_DISPATCHER_DATABASE_URL`, and settings files
`/etc/agentplane-sessions/settings/settings.yaml` / `/etc/agentplane-environments/config.yaml`.
Certificate/Secret resource prefixes follow the new workload names after their readers support the
paths; the URI inside a leaf changes only at S6/S7.

## Inventory verified against source and rendered staging

Baseline: fresh `origin/devel` commit `811320408e8ab3647e060d974820879c373e7401`.
This is a repository inventory, not an assertion about live cluster reconciliation. Recheck against
fresh `origin/devel` at each implementation PR and record its actual pinned images before rollout.
The path census below records concrete and prose references, including tests and inactive evidence;
a match is not an instruction to replace a string.

Rendered files inspected:

- `cluster/k8s/agentplane-staging/agentplane-staging.k8s.yaml` (root resources),
  `cluster/k8s/agentplane-staging/kustomization.yaml`, and `cluster/k8s/agentplane-staging/image-pins/kustomization.yaml`.
- `cluster/generated/agentplane-staging-delivery/agentplane-staging-delivery.k8s.yaml` (Sessions
  workload), and its `kustomization.yaml`, which uses the same staging image-pins Component.
- `cluster/generated/flux-image-automation-forgejo/flux-image-automation-forgejo.k8s.yaml`,
  `cluster/k8s/forgejo-images/forgejo-images.k8s.yaml`,
  `cluster/generated/artifact-generators/artifact-generators.k8s.yaml` and
  `cluster/k8s/flux/kustomizations.k8s.yaml` (publication and Flux ownership).

### Kubernetes resources, routing and publication

All inventory paths are repository-relative; braces group files with a shared prefix.

| Surface                      | Current inventory and source                                                                                                                                                                        | Rendered evidence / migration treatment                                                                                                                                                                                                                                                                                                                                                                                                               |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Sessions workload            | `cluster/cdk8s/agentplane/runner_gateway_workload.py`, `cluster/cdk8s/agentplane/runner_gateway.py`                                                                                                 | Delivery owns `runner-gateway` Deployment, container, ServiceAccount, PDB, Role, RoleBinding, CiliumNetworkPolicy and `runner-gateway-settings` ConfigMap. Its immutable selector is `cdk8s.io/metadata.addr: agentplane-runner-gateway-deployment-c8a3e7b5`; Pod name label is `app.kubernetes.io/name: runner-gateway`. Create a new Deployment; never patch its immutable selector.                                                                |
| Environments workload        | `cluster/cdk8s/agentplane/sandbox_controller.py`, `cluster/cdk8s/agentplane/environment_service.py`                                                                                                 | Root owns corresponding `sandbox-controller` resources, `sandbox-controller-settings` and a ServiceMonitor. Deployment selector is `cdk8s.io/metadata.addr: agentplane-sandbox-controller-deployment-c8a54474`; Pod name label is `app.kubernetes.io/name: sandbox-controller`. Preserve controller exclusivity and existing children.                                                                                                                |
| Services and DNS             | `cluster/cdk8s/agentplane/{runner_gateway,runner_gateway_workload,session_dispatcher,environment_service}.py`; `agentplane/sandboxes/api/constants.go`                                              | Root Services `runner-gateway:8080` and `session-dispatcher:8080` both select `runner-gateway` Pods by name label. `sandbox-controller` Service exposes gRPC `8082` and metrics `8080`, selecting the cdk8s address label. Namespace-qualified `.svc` and `.svc.cluster.local` targets appear in caller settings and templates. There is no `session-dispatcher` Deployment.                                                                          |
| Callers / CONNECT routes     | `cluster/cdk8s/agentplane/{app,app_settings,actions,notifications,central_sampler_workload,egress,workload_routes,platform_routes,environment_sandbox}.py`; `agentplane/sandboxes/api/constants.go` | Root `app-config`, `actions-settings`, `notifications-settings`, `central-sampler-settings`, `egress-go-settings`, `environment-tools-egress-sidecar`, `launcher-relay-egress-sidecar`, `environment-tools-config`, `environment-vm-config` contain targets, exact server URIs or instructions. `workload_routes.py` maps the `session-dispatcher` authority to `runner-gateway`; central proxy allowed destinations must expand before callers move. |
| NetworkPolicy selectors      | `cluster/cdk8s/agentplane/{runner_gateway_workload,sandbox_controller,egress,app,actions,notifications,central_sampler_workload,environment_sandbox,platform_routes}.py`                            | Delivery policy `runner-gateway`; root policies `sandbox-controller`, `egress`, `app`, `actions`, `notifications`, `central-sampler`, `platform-route-clients`, `platform-route-environments`, `environment-tools-listener` select or admit these labels. Expand both directions for new Pods before creating/switching them; a new DNS name alone grants no Cilium reachability.                                                                     |
| Binary / image entrypoints   | `agentplane/{session_dispatcher,environments}/BUILD.bazel` and their `cmd/BUILD.bazel`                                                                                                              | Executables and OCI entrypoints `/usr/local/bin/runner-gateway` and `/usr/local/bin/sandbox-controller`; Environments also has `agentplane-sandbox-controller:latest` loader tag. Update runfile consumers with binary targets, not with a deployment-only edit.                                                                                                                                                                                      |
| Publication / automation     | `devinfra/ci/image_targets.json`; `cluster/cdk8s/forgejo_registry/image_automation.py`                                                                                                              | CI maps old image repository names to `//agentplane/session_dispatcher:image` and `//agentplane/environments:image`. Rendered ImageRepository/ImagePolicy pairs and webhook Receiver package filters name `agentplane-runner-gateway` / `agentplane-sandbox-controller`. Shared ImageUpdateAutomation owns tag updates. Publish new repositories before changing pins; retain old repositories and rollback digests.                                  |
| Image pins / artifact owners | `cluster/cdk8s/agentplane/staging.py`, `cluster/cdk8s/generate_manifests.py`; staging `image-pins/kustomization.yaml`                                                                               | Both Flux owners share pins for `git.allegedly.works/ducktape-ci/agentplane-{runner-gateway,sandbox-controller}` with `flux-system:agentplane-…:tag` Setters markers. Raw generated manifests use `:unset`; effective deployment needs the Component. Keep `agentplane-staging` and `agentplane-staging-delivery` owners; no ownership transfer or accidental Flux prune of Services.                                                                 |
| RBAC and leader Lease        | `cluster/cdk8s/agentplane/{runner_gateway_workload,sandbox_controller}.py`; `agentplane/environments/cmd/main.go`                                                                                   | Old Roles bind old ServiceAccounts. Environments additionally manages children, credentials, status and coordination Leases. Leader election uses `agentplane-sandbox-controller`; no precreated Lease object is rendered. Keep that Lease ID across overlap to avoid two reconciler leaders. Sessions' delivery lease is PostgreSQL-backed, not a namespaced Kubernetes Lease.                                                                       |
| Field ownership              | `agentplane/environments/internal/controller/apply.go`; `agentplane/environments/access/policy.go`                                                                                                  | SSA managers `agentplane-sandbox-controller` and `agentplane-runner-gateway` respectively. Retain them: changing field ownership is a distinct behavior change, unnecessary for deployed names.                                                                                                                                                                                                                                                       |

The requested `agentplane/README.md` Mermaid inventory needs one correction at this baseline:
that README contains service prose and links to `agentplane/docs/system_overview.md`; it has no
Mermaid block. The latter owns the overview and focused Mermaid diagrams (including `gw`,
`sandboxctl`, `sidecar` and `relay` nodes). Update both the README service list and those diagrams
when the deployment change lands, along with `agentplane/docs/runner_control.md` and
`agentplane/session_instructions/platform_instructions.j2`. The current instructions are also
embedded in rendered Runtime/sampler ConfigMaps; changing the template does not rewrite running
harness instructions.

### Authentication, certificates, tokens and Identity resources

| Surface                                | Verified current state and paths                                                                                                                                                                                                                                                               | Treatment                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| TLS identity grammar / pins            | `agentplane/net/peertls/{config.py,credentials.go,client_tls.go}`; server wiring `agentplane/session_dispatcher/internal/app/server.go`, `agentplane/environments/internal/serving/server.go`                                                                                                  | Dispatcher listener uses `spiffe://agentplane/session-dispatcher`; Environments listener uses `spiffe://agentplane/sandbox-controller`. Python server-URI grammar does not yet accept `sessions` or `environments` as server pins. The presence of a caller constant is not receiver support. Expand validators and exact TLS peer checks first.                                                                                                                                                                                                                                                                  |
| Sessions leaves / mount paths          | `cluster/cdk8s/agentplane/{runner_gateway_workload,workload_tls}.py`; delivery manifest                                                                                                                                                                                                        | Certificates `runner-gateway-workload` → Secret `runner-gateway-workload-tls`, URI `spiffe://agentplane/session-dispatcher`; `runner-gateway-sessions-client-workload` → `runner-gateway-sessions-client-workload-tls`, URI `spiffe://agentplane/sessions`. Serving files use `/run/agentplane/workload-tls/{tls.crt,tls.key}`; client mount `/run/agentplane/runner-gateway-sessions-client-tls`. Rename resources by creating replacement leaves; retain old Secrets until rollback closes.                                                                                                                     |
| Environments leaves / mounts           | `cluster/cdk8s/agentplane/{sandbox_controller,workload_tls}.py`; root manifest                                                                                                                                                                                                                 | `sandbox-controller-client-workload` → `sandbox-controller-client-workload-tls`, URI `spiffe://agentplane/sandbox-controller`, mount `/run/agentplane/sandbox-controller-client-tls`; `sandbox-controller-runtime-proxy-client-workload` → matching `-tls` Secret, URI `spiffe://agentplane/runtime-proxy`, mount `/run/agentplane/sandbox-controller-runtime-proxy-client-tls`. The first leaf serves both listener and observer today; split those uses before changing only the server URI. All four Certificates have `24h` duration, `8h` renewBefore and client/server EKUs; unchanged CA and trust bundle. |
| Runtime observer / proxy admission     | `agentplane/env/runtime/control/server.go`; `agentplane/environments/internal/{runtimecontrol/observer.go,serving/runtime_proxy_config.go}`; `agentplane/environments/runtimeproxy/`; `agentplane/env/runtime/SPEC.md`                                                                         | Runtime TLS accepts `runtime-proxy` and `sandbox-controller`; methods separately require the proxy or observe-only controller identity. Neither Sessions nor old `runner-gateway` has direct Runtime authority. Controller self-calls use `runtime_proxy.controller_callers`. Do not replace a trust constant globally or broaden observer privileges.                                                                                                                                                                                                                                                            |
| Other exact role grants                | `cluster/cdk8s/agentplane/{sandbox_controller,runner_gateway_workload,central_sampler_workload}.py`; `agentplane/{session_dispatcher,central_sampler,environments}/settings.py` and Go config validators                                                                                       | Sessions already calls Environments and sampler as `spiffe://agentplane/sessions`; keep that caller ID. Environments self-calls and observer use the controller ID. Preserve independent reader, manager, launcher, operator, session, controller and executor method grants.                                                                                                                                                                                                                                                                                                                                     |
| Token audiences                        | `cluster/cdk8s/agentplane/session_dispatcher.py` has residual `TOKEN_AUDIENCE = NAME` (`session-dispatcher`); `agentplane/{session_dispatcher,environments}/settings.py`; root/delivery YAML                                                                                                   | No destination-audience ServiceAccount token projection or Sessions/Environments bearer audience is rendered. Both serving configs are SVID-only. Existing `agentplane-staging-actions` audience entries belong to Actions, not this rename. No new audience or TokenReview permission is proposed. If re-inventory finds a live old audience, stop that cutover: receiver must accept both exact audiences first, then switch projections, then remove old audience after expiry and last reader. Never revive bearer admission for naming.                                                                      |
| Token paths / bootstrap files          | `agentplane/app/settings.py`, `agentplane/notification_service/settings.py`, `agentplane/{session_dispatcher,environments}/settings.py`, their Go loaders; `agentplane/egress/sidecar_config.py`                                                                                               | Residual optional token settings include `runner_token_file`, `runner_gateway_token_file`, `session_dispatcher_token_file`, sampler `gateway_token_file`, nested outbound `token_file`. App defaults include `/var/run/secrets/agentplane-{runner-gateway,session-dispatcher}/token`; deployed SVID config omits them. Automatic Kubernetes API tokens remain under `/var/run/secrets/kubernetes.io/serviceaccount/`. Do not rename Kubernetes API token paths or repurpose them as peer tokens.                                                                                                                  |
| Agent Identity / workload certificates | `cluster/cdk8s/agentplane/{environment_sandbox,launcher_relay,workload_certificate_admission}.py`; `agentplane/environments/internal/controller/environment_runtime_credential.go`; `agentplane/policy_controller/internal/rpc/sandbox_bindings.go`; `cluster/cdk8s/haku_ci/egress_gateway.py` | No root/delivery `Identity` object is named `runner-gateway` or `sandbox-controller`: service SVIDs are distinct from agent Identities. Sandbox credential records, Identity names/UIDs, credential Secret names, Sandbox SVID grammar, cert issuer, CA bundles, haku-ci standing Identity and PolicyBindings stay. Relay and Runtime share the Sandbox credential; launcher admission must still fence the accepted VMI UID. Check certificate-admission rules before minting renamed service leaves.                                                                                                            |

### Settings, observability, stored data and fixtures

| Surface                        | Paths and current names                                                                                                                                                                                                                                                                                                                         | Proposed treatment                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Process settings               | `agentplane/session_dispatcher/{settings.py,internal/app/config.go}`; `agentplane/environments/{settings.py,cmd/config.go}`; `cluster/cdk8s/agentplane/{runner_gateway_workload,sandbox_controller}.py`                                                                                                                                         | Prefixes `AGENTPLANE_RUNNER_GATEWAY_`, `AGENTPLANE_SANDBOX_CONTROLLER_`; ConfigMap file paths `/etc/agentplane-runner-gateway/settings/settings.yaml`, `/etc/agentplane-sandbox-controller/config.yaml`. New env/file names need an image accepting them before configuration changes; retain strict unknown-key rejection.                                                                                                                                                                                                                                                                                                                                                                                             |
| YAML keys carrying old roles   | Same service settings/loaders; `agentplane/app/{settings.py,internal/settings/config.go}`; `agentplane/notification_service/{settings.py,config.go}`; `agentplane/central_sampler/{settings.py,internal/settings/}`; `agentplane/action_service/{settings.py,cmd/config.go}`                                                                    | Sessions has `gateway_tls`, `runner_control_target_template`, `runner_token_file`, `runner_request_timeout_seconds`. Environments has `runner_gateway_{target,tls,token_file}` and `runner_security`. App has `runner_gateway_{target,tls,token_file,request_timeout_s,lifecycle_timeout_s}` and `session_dispatcher_{target,tls,token_file,request_timeout_s}`. Notifications has nested `runner_gateway`; sampler has `gateway_{target,tls,token_file}`; Actions uses typed endpoint `target`/`tls`/`token_file`. Keep YAML keys in this migration; only their destination values move. Semantic settings renaming can be accepted separately, with a full typed schema map, image-first expansion and removal gates. |
| Metrics / telemetry            | `agentplane/session_dispatcher/internal/app/server.go` initializes `runner-gateway`; `agentplane/environments/cmd/main.go` initializes `sandbox-controller`; `cluster/cdk8s/agentplane/{service_workload,telemetry,sandbox_controller,sandbox_pod}.py`; `agentplane/observability/`; `cluster/cdk8s/monitoring/alloy.py`                        | Rendered `OTEL_SERVICE_NAME` is `runner-gateway` / `sandbox-controller`; relay uses `agentplane-egress-sidecar`. Environments ServiceMonitor `sandbox-controller` selects its Service label. Preserve metric instrument names and units; change resource labels with dual-name query coverage and avoid scraping the same endpoint twice.                                                                                                                                                                                                                                                                                                                                                                               |
| Dashboards / alerts            | Searched `cluster/cdk8s/monitoring/` and rendered monitoring trees for concrete old service names and underscore/CamelCase variants                                                                                                                                                                                                             | No service-specific dashboard or alert match for the two binary names was found; generic Kubernetes/OTEL queries can still follow changed workload/job labels. Verify live dashboards and alert evaluations before removing old labels; externally managed queries are not enumerated by a source search.                                                                                                                                                                                                                                                                                                                                                                                                               |
| PostgreSQL / Goose             | `agentplane/session_dispatcher/migrations/{migrations.go,dispatcher/00014_baseline.sql,test_migrations.py,upgrade_integration_test.go}`; `cluster/cdk8s/agentplane/{database,runner_gateway_workload}.py`; root YAML                                                                                                                            | CNPG Database resource `session-dispatcher`, SQL database/owner `session_dispatcher`, Secret/ExternalSecret `postgres-session-dispatcher` and generator `postgres-session-dispatcher-generator`; Goose table `runner_gateway_dispatcher_goose_version`, advisory lock `0x4453_5043`. Keep all, including sequence/history references in landed SQL. Do not create an empty history table or rerun baselines. Environments has no corresponding Goose history table.                                                                                                                                                                                                                                                     |
| Wire and retained strings      | `agentplane/session_dispatcher/dispatcher/api/session/session_dispatcher.proto`, `dispatcher/internal/notification/dispatcher_notification.proto`, `agentplane/session_dispatcher/dispatcher/`, `agentplane/proto/`                                                                                                                             | Keep protobuf package `agentplane.runner_gateway.dispatcher`, service/method names and method routing/authorization strings, field numbers, notification channel names, retained Session/dispatch rows and strings such as `DELIVERING_TO_RUNNER`. `PROTO_PACKAGE_ALIGNMENT` is separate. No migration edits or data drops.                                                                                                                                                                                                                                                                                                                                                                                             |
| Test images / fixtures         | `agentplane/system_tests/k3s/{defs.bzl,test_pinned_images.py}`, `agentplane/system_tests/deployment/{fixture_chart.py,test_render.py,BUILD.bazel}`, `agentplane/system_tests/{driver,scenarios}/`, `agentplane/testing/{native_gateway_fixtures.py,native_service_fixtures.py,tls.py}`, service-local `testing/`, `agentplane/env/e2e/testing/` | Update image maps, binary runfiles, generated fixtures, fake DNS, exact trust/denial cases, credential mounts, diagnostics/log collection, restart/reconnect and in-flight dispatch scenarios in their owning implementation PRs. Keep test data unmistakably fake and shared support under `agentplane/testing`. Never change production wire values just to make a fixture pass. Full matching paths are in the census.                                                                                                                                                                                                                                                                                               |
| Docs / diagrams / instructions | `agentplane/README.md`; `agentplane/docs/system_overview.md`; component README/SPEC files; `agentplane/env/runtime/docs/{config,control}.md`; `agentplane/session_instructions/platform_instructions.j2`; active `plans/` and debug references in census                                                                                        | Fresh README links its system overview; it contains no Mermaid block at this baseline. The Mermaid diagrams are in `docs/system_overview.md`: update the Gateway/controller nodes, click labels and traffic descriptions there, and the README Services list/link in the same implementation PR. Check README again in case the diagram moves back. Preserve historical evidence/artifact checksums; correct active instructions, including their stale Gateway traffic description.                                                                                                                                                                                                                                    |

## Ordered rollout for RUNNER_GATEWAY_RENAME

Every numbered step is a separate deployable gate, not a promise that merging adjacent PRs in order
makes Flux apply them in order. Observe reconciliation and pinned image digests before proceeding.
`BREAKS RUNNING WORK: YES` flags any step whose mistake can interrupt a running Sandbox or Session;
it is a warning requiring evidence, not permission to restart, delete or lose it. Rollback always
means a reviewed revert/pin change through the existing Flux owner, followed by the stated check.
Never reset a database, replay an uncertain command, or recreate a Sandbox to finish this rename.
Compatibility changes below are behavior migrations with dated `CLEANUP` gates, not refactors.

### S0 — approve scope and capture rollback state

**BREAKS RUNNING WORK: NO (read-only).** Resolve the decisions below; re-inventory fresh devel and
actual root/delivery revisions, digests, resource UIDs, Service endpoints, active/suspended/checkpointed
Runtime versions, dispatch state and Lease holder. Record exact rollback pins and data backup/restore
evidence under the owning investigation location. No source/manifest mutation before approval.

**Verify:** each caller/receiver and Flux owner has an assigned cutover and an available old digest;
ordinary native/central Sessions and Pod/KubeVirt/Firecracker environments have a baseline.
**Rollback:** discard the proposed rollout; nothing deployed.

### S1 — publish images and expand readers without selecting new names

**BREAKS RUNNING WORK: YES (reader/trust changes and process rolls).** Publish the new image
repositories and image automation, keeping existing pins. Add support for the approved bootstrap env
names and future mount paths to binaries before any manifests use them. Accept either bootstrap name;
reject conflicting values. Keep strict YAML validation and existing YAML keys. If security rename is
accepted, expand all receiver/client validators and exact peer checks for old/new server URIs and
controller caller IDs, scoped to the same existing methods. Do not yet present a new caller ID or
server certificate. Publish Runtime images with dual observer trust before considering S7.
Do not add an audience where none exists; any discovered live token audience follows the dual-audience
gate in the inventory. Pin/run expanded readers under the old configuration before S2.

**Verify:** CI exercises old-config startup and new-config startup, both exact peer identities and
wrong-peer/method rejection, old/new mixed-version RPC, restart/reconnect and queued/in-flight delivery.
All affected Deployments run the expanded image digest; publication alone is insufficient.
**Rollback:** old config still works; restore prior digests, then verify delivery and observation.
Do not roll back a reader after a later step has introduced new config or credentials it cannot read.

### S2 — grant new accounts and permit new network identities

**BREAKS RUNNING WORK: YES (RBAC and Cilium).** Create new ServiceAccounts and narrowly identical
RoleBindings/roles for `sessions` and `environments`. Keep old grants. Add network selectors admitting
both old and new Pods, including proxy destinations and return paths, before Pods use new labels.
Keep both accounts on the same controller Lease, database and credential authority. Expand dashboard/alert queries to both workload/OTEL names before telemetry changes;
keep one scrape per endpoint. Create and wait
for new named Certificate/Secret resources with **old URIs** first; check certificate admission and
mount readability. Keep old certificate resources. No caller switches here.

**Verify:** exact Kubernetes verbs/resources/subresources work for each new account, including
Lease get/create/update and Sandbox status/finalizer ownership; denied verbs remain denied. Rendered
selector relationships and old traffic remain valid. New Certificates are Ready under the same CA.
**Rollback:** remove only unused new grants/resources after confirming no new Pods exist; old calls
and reconciliation must remain healthy. Never remove permissions while their account still runs.

### S3 — add new Services alongside old Services

**BREAKS RUNNING WORK: YES (routing configuration).** Add root-owned `sessions:8080` selecting the
existing Gateway Pods and `environments:8082` selecting existing controller Pods; retain both old
Sessions Services and old controller Service. Expand central CONNECT allowed authorities before new
clients dial them. Keep metrics scrape singular. Use old server SVIDs until S6; DNS and TLS identity
are independent. Change no Flux resource owner.

**Verify:** old/new DNS resolves to intended ready EndpointSlices; authenticated RPC through every
old/new Service succeeds from each restricted caller, while wrong identity/direct forbidden traffic
still fails. Check gRPC watch/reconnect, not health alone.
**Rollback:** callers still use old DNS; remove new Services/routes only after confirming none moved.

### S4 — run the renamed workloads and bootstrap config

**BREAKS RUNNING WORK: YES (controller leadership, credential mounts and in-flight dispatch).**
Create the new Deployments using published/pinned images, granted accounts, existing YAML keys,
new bootstrap env/file names and old SVIDs. Keep the old controller's Lease ID, SSA managers, Sandbox
owners/UIDs, PVCs, WorkloadCredentials and queue DB. Old/new Sessions replicas share durable dispatch
leases; do not invent a second queue or reset delivery. Roll one binary at a time. Extend all three
Sessions Service selectors and both Environments Service selectors using an explicit shared routing
label added to old/new Pods first; avoid overlapping Deployment selectors. Keep both routes available
while shifting endpoints, and retain old Deployments as rollback capacity until validation finishes.
The Environments listener can serve on every replica; only the existing Lease holder reconciles.
Move telemetry names after new endpoints are observable, keeping dual-name queries for the
overlap/rollback window. Per the execution release, retain the existing ServiceMonitor on the
old Service until S8; the old Service must continue selecting the active Pods so each endpoint
is scraped once.

**Verify:** old/new Services reach new replicas; one reconciler Lease holder, unchanged child/PVC and
credential identities, observer binding, native/central dispatch idempotency and watch catch-up across
replica termination. Exercise long in-flight work during the switch and confirm no duplicate effects.
**Rollback:** restore Service endpoint selection to old healthy Pods and scale old workloads back;
then roll back new bootstrap config before any old binary pin. Verify queue convergence and Runtime
observation; do not restart the Runtime or replay commands with unknown outcomes.

### S5 — move callers and future Sandbox templates to new DNS

**BREAKS RUNNING WORK: YES (platform routes, launcher relay configuration and frozen clients).**
Move App, Actions, Notifications, sampler and Sessions' Environments target in separate observable
batches. Then change Pod, KubeVirt launcher and Firecracker template routes/instructions for future
launches. Receiver images, grants, new Services and expanded central proxy routes already exist.
Keep server pins on old SVIDs. Keep old DNS/CONNECT authorities for running and checkpointed
Sandboxes, durable queued targets, old relay configs and supported rollback images. Do not mutate
live Pods, force resumes, or recreate VMs to make a caller census empty.

**Verify:** each batch reaches the new Service from restricted networks, receives intended authz and
can reconnect watches; native/central Sessions, suspend/resume, Pod/VM relay and executor work.
Inventory live and suspended callers, not merely templates; dispatch backlog/in-flight work converges.
**Rollback:** restore caller/template destinations to old Services; preserve both server endpoints and
route allowlists until rollback reconciliation finishes, then verify existing Sessions continue.

### S6 — optional server SVID and certificate cutover

**BREAKS RUNNING WORK: YES (TLS pins in existing clients/relays).** Only if approved: first prove all
clients that may use each listener, including old running/suspended relay configurations and rollback
binaries, accept both exact server IDs. If they do not, keep old SVIDs and defer this step. Create
replacement leaves with new URIs after expanded validators are deployed, then switch listener
certificates and local expected-URI config together. For Environments, first separate serving from
outbound observer credentials: keep the observer on its old leaf/URI and proxy on `runtime-proxy`.
Do not put multiple URI SANs into a leaf as a shortcut around single-identity verification.

**Verify:** old/new Service names and every supported client version authenticate the new leaf;
wrong URI, CA, EKU and method remain rejected. Confirm observer and self-call paths independently.
**Rollback:** restore old serving leaf and expected local URI while dual client pins remain deployed;
verify watches/reconnect and dispatch. Preserve old leaves/secrets throughout the rollback window.

### S7 — optional controller observer caller SVID cutover

**BREAKS RUNNING WORK: YES (direct trust in running Runtimes; may block observation, suspend and dispatch).**
This gate is independent of S6. Every affected Runtime must already run dual-trust code for
`sandbox-controller` and `environments` at TLS **and** observe-only method admission, including
suspended VM snapshots, restored Firecracker state and supported rollback Runtime images.
Expanded Environments `controller_callers`, config validators and self-call clients must be live
first. Only then switch the controller's outbound leaf/URI to `environments`. Preserve proxy/observer
role separation and the Runtime's denial of Sessions/legacy Gateway direct control.
If running/checkpointed Runtimes cannot adopt this safely, retain the old observer URI; operator
acceptance of a deployment rename does not authorize terminating those Runtimes.

**Verify:** old/new observer credentials bind the same incarnation, wrong role cannot control it,
resume from each snapshot type reconnects, and observation, quiesce and native dispatch remain sound.
**Rollback:** restore the old observer leaf and caller config; dual Runtime trust remains. Confirm
binding/watch recovery without replacing a Sandbox or invalidating its credential.

### S8 — retire old workloads, then old names and compatibility

**BREAKS RUNNING WORK: YES (pruning active routes, grants or credentials can strand old Sandboxes).**
First scale old Deployments to zero after S4/S5 evidence; retain their manifests/pins for the agreed
rollback window. Before removing old Services, prove no live or suspended/checkpointed caller,
queued target, reconnecting connection, launch configuration or supported rollback image needs them.
New template deployment alone is not that proof. Before deleting old SVID acceptance, additionally
prove S6/S7 complete and no old leaf is in use; wait for old credentials to expire, including the
rendered `24h` certificate validity and any later renewal. Stop old issuance before starting that
expiry observation. Remove old caller config first, then now-unused reader fields/compatibility in
a later image. Remove RBAC bindings only after old Pods/callers are gone. Remove old image automation,
pins, Certificates and Secrets last. Keep Lease ID, SSA managers, DB/history/wire/stored names.

**Verify:** fresh source/rendered search accounts for every remaining old name as intentional;
old EndpointSlices and account users are empty, new Services carry traffic, no auth failures or
missing telemetry, all preservation/continuity scenarios pass. Update maintained docs and diagrams.
**Rollback:** before contraction, restore old routes/workloads/pins. After contraction, reintroduce
old grants, trust readers, certificates and Services **in that order before** restoring old callers;
verify their endpoints and RPCs. A code-only revert is insufficient once config has contracted.
If the last-caller or snapshot evidence cannot be obtained, leave the corresponding cleanup pending.

## EGRESS_RELAY_RENAME — separate approval

The operator requested “egress sidecar” → “relay” on 2026-10-16. Recommend `relay` for the binary,
container and source role, image `agentplane-egress-relay`, bootstrap
`AGENTPLANE_EGRESS_RELAY_CONFIG_FILE` and `/etc/agentplane-egress-relay/settings.yaml`.
This is a local relay to the existing central egress proxy; do not rename that proxy, its
`spiffe://agentplane/egress-proxy`, Sandbox workload IDs or Identity resources. No new Service/SVID
is needed just because a sidecar becomes a relay. Keep already accurate `launcher-relay`,
`haku-ci-egress`, their DNS, and all proxy/listener ports and addresses.

Additional inventory (the census includes exact paths):

- Binary/entrypoint and shared Go/Python settings: `agentplane/egress/BUILD.bazel`,
  `agentplane/egress/sidecar/{BUILD.bazel,cmd/BUILD.bazel,internal/relay/config.go}`,
  `agentplane/egress/sidecar_config.py`, `agentplane/egress/testing/sidecar_process.py`.
  Existing binary `/usr/local/bin/egress-sidecar`, container `egress-sidecar`, bootstrap
  `AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE`, settings path `/etc/agentplane-egress-sidecar/settings.yaml`.
- Publication/pins: the same CI image map, Forgejo automation/Receiver and staging Component above
  name `agentplane-egress-sidecar`; `agentplane/system_tests/k3s/defs.bzl` supplies its fixture image.
- Templates/injection: `cluster/cdk8s/agentplane/{sandbox_pod,command_sandbox,environment_sandbox,launcher_relay}.py`,
  `agentplane/environments/internal/controller/environment_runtime_credential.go`,
  `cluster/cdk8s/haku_ci/egress_gateway.py`. Root YAML contains ConfigMaps
  `environment-tools-egress-sidecar`, `launcher-relay-egress-sidecar`, `sandbox-egress-sidecar`,
  SandboxTemplate `environment-tools`, ClusterPolicy `agentplane-staging-launcher-relay`,
  `sandbox` / `sandbox-build` PodTemplates, and Deployment `haku-ci-egress` using the old image/container.
  Credential injection looks up the container name: update it coherently with template/injector
  readers, including init-container/native-sidecar handling.
- Mounted volume `egress-sidecar-settings`; workload TLS projection, launcher validation and readiness
  probes; `cluster/cdk8s/agentplane/{environment_guest,environment_vm,environment_firecracker}.py` and
  `agentplane/firecracker/host/credentials.go` must preserve credential/guest wiring even when they
  use generic relay names instead of the old literal. KubeVirt guest address is `10.0.2.1`, local Pod
  proxy `127.0.0.1`; Firecracker addresses are supplied by its launcher. Retain HTTP `3128`, shared
  platform gRPC `3130`, CONNECT authorities and traffic semantics.
- `cluster/cdk8s/{public_coder/egress.py,haku/ui_egress.py}` contain other “sidecar” terminology;
  `cluster/generated/agents/agent-sandbox/controller/patches.k8s.yaml`,
  `cluster/parked/public-coder-agent/app/` and `cluster/k8s/agents/agent-sandbox/README.md` contain
  inactive/generated consumers. Determine ownership before changing them; do not rename unrelated
  Haku/mitmproxy components on a substring match. `agentplane/x/firecracker_spike/` and artifact
  checksum evidence stay historical evidence. `cluster/cdk8s/monitoring/alloy.py` has prose about
  sidecars; telemetry source `sandbox_pod.py` uses `agentplane-egress-sidecar`, and
  `agentplane/egress/sidecar/internal/relay/runtime.go` initializes tracing as `egress-sidecar`.

Run R0–R4 independently of S0–S8. Sharing a helper does not create a deployment dependency.

| Step | Running-work flag and ordered change                                                                                                                                                                                                                                                                                                                                            | Verification                                                                                                                                                                                                                                                            | Rollback                                                                                                                                                 |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R0   | **BREAKS RUNNING WORK: NO.** Accept relay scope; inventory live Pod/init-container/launcher and standing relay users, old image digests and checkpointed guests.                                                                                                                                                                                                                | Assign every image/settings/container-name reader and record the old manifest/pin.                                                                                                                                                                                      | No mutation.                                                                                                                                             |
| R1   | **BREAKS RUNNING WORK: YES.** Publish new image/automation first; deploy config readers accepting old/new bootstrap names and injector/controller readers recognizing old/new container names, still producing old templates. Preserve exact credential isolation.                                                                                                              | Old/new fake configurations start; credential injection covers regular and native-sidecar containers and still excludes user containers; image is pullable and reader image is pinned.                                                                                  | Old templates/config remain usable: restore reader pins after checking no new container/config is in use.                                                |
| R2   | **BREAKS RUNNING WORK: YES (launcher and credential injection).** Add new ConfigMaps/paths, then switch future Pod/command-box templates, KubeVirt injection and Firecracker launcher wiring with all callers in one coherent source change. Apply the approved `relay` container name only after R1 readers are running. Do not mutate existing Pods or guest relay addresses. | Render and system-test Pod/KubeVirt/Firecracker startup, readiness, proxy/3130 routes, credential mounts, native sidecar shutdown and suspend/resume. Check admission failure/UID fencing as well as success. Staging is the first test of Flux/Cilium/Kyverno objects. | Restore templates/injection and old ConfigMaps/pins; existing old and new containers remain understood. Verify new launch plus existing Session traffic. |
| R3   | **BREAKS RUNNING WORK: YES (standing relay restart and active proxy streams).** Roll `haku-ci-egress` and other confirmed standing consumers after new image/settings publication, maintaining their existing Service addresses, Identity grants and capacity. Move telemetry labels with dual queries; update docs and fixtures.                                               | Build/network requests, long-lived CONNECT/gRPC streams, all affected Identity authorization and both telemetry names work; verify actual controller account/container wiring.                                                                                          | Restore old image/config with R1 readers retained; verify Service endpoints and proxy requests. Do not rotate the standing Identity.                     |
| R4   | **BREAKS RUNNING WORK: YES.** Remove old ConfigMaps, image policies/pins and compatibility only after no live or suspended launch/relay, rollback image or credential injector needs old names. Stop old publishers after consumers move; do not force-recreate Sandboxes.                                                                                                      | Census plus live/snapshot inventory proves last reader gone; credential injection/egress/telemetry remain healthy.                                                                                                                                                      | Restore old ConfigMaps, readers and image availability before reverting any template or caller. Otherwise hold cleanup.                                  |

## Operator decisions and remaining holds

1. **Accepted:** `sessions` / `environments`, matching image names and bootstrap prefixes;
   existing YAML keys, DB names, Lease and field managers stay.
2. **Unreleased:** server SVID changes S6 and observer caller change S7. Keep all current
   SPIFFE IDs, `runtime-proxy` and method roles. Any future identity release requires its
   own live/frozen client compatibility evidence.
3. **Chosen by the coordinator:** S0's pinned digests are supported rollback images; retain
   old Deployment manifests and pins until S8. The coordinator certifies the complete
   live/suspended/checkpointed last-caller census before removal. No forced Sandbox/Session
   interruption or data deletion is included.
4. **Unreleased:** R0–R4 for the relay. Retain its image, bootstrap/container names,
   proxy identity, standing Identity names, listener addresses and unrelated sidecars.

Each implementation PR states its gate, exact checks, rollback and anything deferred. Run applicable
Bazel/system tests in CI; staging remains the first deployment of Flux, Cilium, cert-manager and
Kyverno objects without system-test equivalents. The operator merges by hand. Retire this active
plan only after accepted steps and their cleanup land; promote enduring contracts into component
SPECs/authentication/operations docs first.

## Reference census

File-level census at the baseline above. `G` = Runner Gateway spellings, `C` = Sandbox Controller
spellings, `R` = egress sidecar spellings (hyphen, underscore, spaces, case-insensitive and CamelCase).
Paths are repository-relative. Includes source, BUILD, generated/parked manifests, docs, fixtures,
active plans and diagnostic evidence; wire/stored/history/evidence matches remain intentionally.
Generic `SessionDispatcher` references are not all deployed-name references; its stable wire/API
names are inventoried above and retained. Use fresh `rg` searches for those names when implementing.

Reproduce the name census with `rg -il --hidden -g '!.git'` and these expressions:

```text
runner[-_ ]gateway|RunnerGateway
sandbox[-_ ]controller|SandboxController
egress[-_ ]sidecar|EgressSidecar
```

| Path                                                                                                          | Names   |
| ------------------------------------------------------------------------------------------------------------- | ------- |
| `agentplane/README.md`                                                                                        | G, C    |
| `agentplane/action_service/README.md`                                                                         | G, C    |
| `agentplane/action_service/SPEC.md`                                                                           | R       |
| `agentplane/action_service/cmd/config.go`                                                                     | C       |
| `agentplane/action_service/cmd/config_test.go`                                                                | C       |
| `agentplane/action_service/cmd/sandbox_groups_test.go`                                                        | R       |
| `agentplane/action_service/docs/agents/llms.txt`                                                              | R       |
| `agentplane/action_service/orchestration_v1/launch.go`                                                        | C       |
| `agentplane/action_service/sandbox/README.md`                                                                 | R       |
| `agentplane/action_service/sandbox/fixture_test.go`                                                           | R       |
| `agentplane/action_service/sandbox/inventory.go`                                                              | R       |
| `agentplane/action_service/sandbox/inventory_test.go`                                                         | R       |
| `agentplane/action_service/settings.py`                                                                       | C       |
| `agentplane/action_service/test_settings.py`                                                                  | C       |
| `agentplane/app/README.md`                                                                                    | G, C    |
| `agentplane/app/app_service.proto`                                                                            | G       |
| `agentplane/app/debug/2026_11_12_thread_window_projection_gap.md`                                             | G       |
| `agentplane/app/frontend/harness/harness.tsx`                                                                 | G       |
| `agentplane/app/internal/server/runtime.go`                                                                   | G       |
| `agentplane/app/internal/server/server_test.go`                                                               | G       |
| `agentplane/app/internal/sessionclient/runtime_diagnostics_test.go`                                           | C       |
| `agentplane/app/internal/settings/config.go`                                                                  | G, C    |
| `agentplane/app/internal/settings/config_test.go`                                                             | G, C    |
| `agentplane/app/settings.py`                                                                                  | G, C    |
| `agentplane/app/test_bridge.py`                                                                               | G       |
| `agentplane/app/test_history_gateway.py`                                                                      | G       |
| `agentplane/app/test_settings.py`                                                                             | G, C    |
| `agentplane/app/testing/gateway_fixtures.py`                                                                  | G       |
| `agentplane/app/testing/replication_process.py`                                                               | G       |
| `agentplane/app/testing/replication_source.py`                                                                | G       |
| `agentplane/app/testing/runtime_fixtures.py`                                                                  | G, C    |
| `agentplane/app/testing/thread_browser.py`                                                                    | G       |
| `agentplane/central_sampler/README.md`                                                                        | C       |
| `agentplane/central_sampler/SPEC.md`                                                                          | G, C    |
| `agentplane/central_sampler/cmd/main_test.go`                                                                 | G       |
| `agentplane/central_sampler/internal/settings/settings_test.go`                                               | G, C    |
| `agentplane/central_sampler/internal/settings/validation.go`                                                  | C       |
| `agentplane/central_sampler/settings.py`                                                                      | C       |
| `agentplane/central_sampler/test_gateway_integration.py`                                                      | G, C    |
| `agentplane/central_sampler/test_settings.py`                                                                 | G, C    |
| `agentplane/cli/internal/actions/wait.go`                                                                     | R       |
| `agentplane/cli/internal/invocation/errors.go`                                                                | R       |
| `agentplane/debug/2026_10_30_kubernetes_ownership_review.md`                                                  | G, C    |
| `agentplane/debug/actions_service_grants_vm_boot.md`                                                          | C       |
| `agentplane/debug/dispatcher_cleanup_timeout.md`                                                              | C       |
| `agentplane/debug/firecracker_spike_2026_11_02/artifact_sha256sums.txt`                                       | R       |
| `agentplane/debug/resume_credentials_valid.md`                                                                | C       |
| `agentplane/docs/adr_action_sandboxes.md`                                                                     | G, C, R |
| `agentplane/docs/adr_orchestration_v1.md`                                                                     | G       |
| `agentplane/docs/adr_policy_sets.md`                                                                          | C       |
| `agentplane/docs/adr_sandbox_proxy_gateway.md`                                                                | C       |
| `agentplane/docs/adr_sandbox_session_control.md`                                                              | G, C, R |
| `agentplane/docs/agent_cli.md`                                                                                | G, R    |
| `agentplane/docs/buildbuddy_remote_auth.md`                                                                   | R       |
| `agentplane/docs/environment_policy_rollout.md`                                                               | G       |
| `agentplane/docs/firecracker_sandboxes.md`                                                                    | G, C    |
| `agentplane/docs/hooks.md`                                                                                    | G       |
| `agentplane/docs/lessons_learned.md`                                                                          | G, R    |
| `agentplane/docs/runner_control.md`                                                                           | C, R    |
| `agentplane/docs/runner_delivery_and_harness_design.md`                                                       | G       |
| `agentplane/docs/sandbox_identity_cutover_2026_11_12/README.md`                                               | G, C    |
| `agentplane/docs/sandbox_identity_cutover_2026_11_12/evidence.json`                                           | G, C    |
| `agentplane/docs/service_boundaries.md`                                                                       | G, C    |
| `agentplane/docs/system_overview.md`                                                                          | G, C, R |
| `agentplane/docs/thread_layering.md`                                                                          | G, C    |
| `agentplane/docs/thread_view_sync.md`                                                                         | G       |
| `agentplane/docs/vm_sandboxes.md`                                                                             | C       |
| `agentplane/docs/workload_authentication.md`                                                                  | G, C, R |
| `agentplane/egress/BUILD.bazel`                                                                               | R       |
| `agentplane/egress/README.md`                                                                                 | C, R    |
| `agentplane/egress/cmd/admin_grpc_test.go`                                                                    | C       |
| `agentplane/egress/docs/design.md`                                                                            | R       |
| `agentplane/egress/platform_routes.py`                                                                        | R       |
| `agentplane/egress/sidecar/BUILD.bazel`                                                                       | R       |
| `agentplane/egress/sidecar/cmd/BUILD.bazel`                                                                   | R       |
| `agentplane/egress/sidecar/internal/relay/config.go`                                                          | R       |
| `agentplane/egress/sidecar/internal/relay/platform_routes.go`                                                 | R       |
| `agentplane/egress/sidecar/internal/relay/relay.go`                                                           | R       |
| `agentplane/egress/sidecar/internal/relay/runtime.go`                                                         | R       |
| `agentplane/egress/sidecar_config.py`                                                                         | C, R    |
| `agentplane/egress/test_platform_routes.py`                                                                   | R       |
| `agentplane/egress/test_proxy.py`                                                                             | C       |
| `agentplane/egress/test_sidecar.py`                                                                           | R       |
| `agentplane/egress/testing/sidecar_process.py`                                                                | R       |
| `agentplane/env/README.md`                                                                                    | G       |
| `agentplane/env/TODO.md`                                                                                      | C       |
| `agentplane/env/config.py`                                                                                    | R       |
| `agentplane/env/configuration/configuration.go`                                                               | C       |
| `agentplane/env/configuration/resolved.go`                                                                    | R       |
| `agentplane/env/e2e/README.md`                                                                                | G       |
| `agentplane/env/e2e/conftest.py`                                                                              | G       |
| `agentplane/env/e2e/test_codex_shared_process.py`                                                             | G       |
| `agentplane/env/e2e/testing/fixtures.py`                                                                      | G       |
| `agentplane/env/e2e/testing/gateway.py`                                                                       | G, C    |
| `agentplane/env/images/vm.nix`                                                                                | G, C    |
| `agentplane/env/runtime/SPEC.md`                                                                              | G, C    |
| `agentplane/env/runtime/control/server.go`                                                                    | C       |
| `agentplane/env/runtime/docs/config.md`                                                                       | C       |
| `agentplane/env/runtime/docs/control.md`                                                                      | G, C    |
| `agentplane/env/runtime/execution/docs/codex_executor.md`                                                     | C, R    |
| `agentplane/env/runtime/internal/coordinator/forward_test.go`                                                 | C       |
| `agentplane/env/runtime/supervision/docs/supervision.md`                                                      | C       |
| `agentplane/environments/BUILD.bazel`                                                                         | C       |
| `agentplane/environments/README.md`                                                                           | G, C, R |
| `agentplane/environments/SPEC.md`                                                                             | G, C, R |
| `agentplane/environments/access/policy.go`                                                                    | G       |
| `agentplane/environments/cmd/BUILD.bazel`                                                                     | C       |
| `agentplane/environments/cmd/catalog_test.go`                                                                 | C       |
| `agentplane/environments/cmd/config.go`                                                                       | G, C    |
| `agentplane/environments/cmd/config_test.go`                                                                  | G, C    |
| `agentplane/environments/cmd/main.go`                                                                         | G, C    |
| `agentplane/environments/internal/controller/apply.go`                                                        | C       |
| `agentplane/environments/internal/controller/behavior.go`                                                     | C       |
| `agentplane/environments/internal/controller/cache.go`                                                        | C       |
| `agentplane/environments/internal/controller/children_integration_test.go`                                    | G, C    |
| `agentplane/environments/internal/controller/claim.go`                                                        | C       |
| `agentplane/environments/internal/controller/controller.go`                                                   | G, C, R |
| `agentplane/environments/internal/controller/controller_test.go`                                              | C       |
| `agentplane/environments/internal/controller/environment_runtime_credential.go`                               | C, R    |
| `agentplane/environments/internal/controller/environment_runtime_credential_test.go`                          | C, R    |
| `agentplane/environments/internal/controller/firecracker.go`                                                  | C       |
| `agentplane/environments/internal/controller/firecracker_status_reader_timeout_test.go`                       | C       |
| `agentplane/environments/internal/controller/firecracker_suspend.go`                                          | C       |
| `agentplane/environments/internal/controller/firecracker_suspend_test.go`                                     | C       |
| `agentplane/environments/internal/controller/firecracker_test.go`                                             | C       |
| `agentplane/environments/internal/controller/identity.go`                                                     | C       |
| `agentplane/environments/internal/controller/identity_test.go`                                                | C       |
| `agentplane/environments/internal/controller/idle.go`                                                         | C       |
| `agentplane/environments/internal/controller/idle_test.go`                                                    | C       |
| `agentplane/environments/internal/controller/launch.go`                                                       | C       |
| `agentplane/environments/internal/controller/launch_test.go`                                                  | C       |
| `agentplane/environments/internal/controller/quiescer.go`                                                     | C       |
| `agentplane/environments/internal/controller/runtime_facts.go`                                                | C       |
| `agentplane/environments/internal/controller/runtime_facts_test.go`                                           | C       |
| `agentplane/environments/internal/controller/runtime_states.go`                                               | C       |
| `agentplane/environments/internal/controller/runtime_states_test.go`                                          | C       |
| `agentplane/environments/internal/controller/state_volume.go`                                                 | C       |
| `agentplane/environments/internal/controller/state_volume_test.go`                                            | C       |
| `agentplane/environments/internal/controller/vm.go`                                                           | C       |
| `agentplane/environments/internal/controller/vm_test.go`                                                      | C       |
| `agentplane/environments/internal/controller/workspace.go`                                                    | C       |
| `agentplane/environments/internal/controller/workspace_test.go`                                               | C       |
| `agentplane/environments/internal/runtimecontrol/observer_test.go`                                            | C       |
| `agentplane/environments/internal/runtimecontrol/quiescer.go`                                                 | C       |
| `agentplane/environments/internal/runtimecontrol/quiescer_test.go`                                            | C       |
| `agentplane/environments/internal/runtimecontrol/suspender.go`                                                | G       |
| `agentplane/environments/internal/service/README.md`                                                          | C       |
| `agentplane/environments/internal/service/SPEC.md`                                                            | C       |
| `agentplane/environments/internal/service/lifecycle_test.go`                                                  | C       |
| `agentplane/environments/internal/service/service_test.go`                                                    | C       |
| `agentplane/environments/internal/service/watch_test.go`                                                      | C       |
| `agentplane/environments/internal/serving/server.go`                                                          | C       |
| `agentplane/environments/internal/serving/server_test.go`                                                     | C       |
| `agentplane/environments/lifecycle/README.md`                                                                 | C       |
| `agentplane/environments/lifecycle/SPEC.md`                                                                   | C       |
| `agentplane/environments/runtimeproxy/SPEC.md`                                                                | C       |
| `agentplane/environments/runtimeproxy/fixture_test.go`                                                        | C       |
| `agentplane/environments/runtimeproxy/methods_test.go`                                                        | C       |
| `agentplane/environments/runtimeproxy/proxy_test.go`                                                          | C       |
| `agentplane/environments/runtimeproxy/runtime_observer_test.go`                                               | C       |
| `agentplane/environments/settings.py`                                                                         | G, C    |
| `agentplane/environments/test_settings.py`                                                                    | G, C    |
| `agentplane/environments/testing/process.py`                                                                  | C       |
| `agentplane/environments/testing/servicefixture/launcher.go`                                                  | C       |
| `agentplane/environments/v1/environment_service.proto`                                                        | C       |
| `agentplane/firecracker/SPEC.md`                                                                              | C       |
| `agentplane/firecracker/config.py`                                                                            | C       |
| `agentplane/firecracker/host/credentials.go`                                                                  | G       |
| `agentplane/firecracker/host/testing/runtime_probe/main.go`                                                   | C       |
| `agentplane/grpc/calls.py`                                                                                    | G       |
| `agentplane/history_service/README.md`                                                                        | G       |
| `agentplane/history_service/internal/ingress/ingress.go`                                                      | C       |
| `agentplane/identity/credentials/requester/requester.go`                                                      | C       |
| `agentplane/net/peertls/config.py`                                                                            | G, C    |
| `agentplane/net/peertls/credentials.go`                                                                       | G, C    |
| `agentplane/net/peertls/credentials_test.go`                                                                  | C       |
| `agentplane/notification_service/README.md`                                                                   | G, C    |
| `agentplane/notification_service/config.go`                                                                   | G, C    |
| `agentplane/notification_service/config_test.go`                                                              | G, C    |
| `agentplane/notification_service/docs/architecture.md`                                                        | G       |
| `agentplane/notification_service/main.go`                                                                     | G       |
| `agentplane/notification_service/main_test.go`                                                                | G       |
| `agentplane/notification_service/settings.py`                                                                 | G, C    |
| `agentplane/notification_service/test_settings.py`                                                            | G, C    |
| `agentplane/notification_service/testing/process_fixture/driver_test.go`                                      | G       |
| `agentplane/plans/architecture_overview.md`                                                                   | G, C    |
| `agentplane/plans/claude_remote_io.md`                                                                        | G       |
| `agentplane/plans/environment_rules.md`                                                                       | G       |
| `agentplane/plans/identity_and_kubernetes_accounts.md`                                                        | C       |
| `agentplane/plans/kubernetes_decoupling.md`                                                                   | G, C, R |
| `agentplane/plans/multiagent_communication.md`                                                                | G, C    |
| `agentplane/plans/native_harness_interfaces.md`                                                               | C       |
| `agentplane/plans/runtime_proxy_callers.md`                                                                   | C       |
| `agentplane/plans/sampling_outside_sandboxes.md`                                                              | G       |
| `agentplane/plans/session_environment_model.md`                                                               | C       |
| `agentplane/plans/session_runtime_model.md`                                                                   | G, C    |
| `agentplane/plans/task_dag.md`                                                                                | G, C, R |
| `agentplane/plans/task_freezer.md`                                                                            | G, C    |
| `agentplane/policy_controller/SPEC.md`                                                                        | C       |
| `agentplane/policy_controller/internal/rpc/sandbox_bindings.go`                                               | C       |
| `agentplane/policy_controller/internal/rpc/service_test.go`                                                   | C       |
| `agentplane/proto/environment_runtime_state.proto`                                                            | C       |
| `agentplane/sandbox_resource.py`                                                                              | C       |
| `agentplane/sandboxes/README.md`                                                                              | G, C    |
| `agentplane/sandboxes/api/constants.go`                                                                       | C       |
| `agentplane/sandboxes/environmentruntimeclient/client.go`                                                     | G       |
| `agentplane/sandboxes/environmentruntimeclient/client_test.go`                                                | C       |
| `agentplane/sandboxes/sandboxreader/reader_test.go`                                                           | C       |
| `agentplane/sandboxes/testing/kubernetes.py`                                                                  | C       |
| `agentplane/session_dispatcher/BUILD.bazel`                                                                   | G       |
| `agentplane/session_dispatcher/README.md`                                                                     | G, C    |
| `agentplane/session_dispatcher/api.py`                                                                        | G       |
| `agentplane/session_dispatcher/client.py`                                                                     | G, C    |
| `agentplane/session_dispatcher/cmd/BUILD.bazel`                                                               | G       |
| `agentplane/session_dispatcher/conftest.py`                                                                   | G       |
| `agentplane/session_dispatcher/dispatcher/README.md`                                                          | G       |
| `agentplane/session_dispatcher/dispatcher/SPEC.md`                                                            | G       |
| `agentplane/session_dispatcher/dispatcher/api/session/session_dispatcher.proto`                               | G       |
| `agentplane/session_dispatcher/dispatcher/internal/notification/dispatcher_notification.proto`                | G       |
| `agentplane/session_dispatcher/internal/app/config.go`                                                        | G, C    |
| `agentplane/session_dispatcher/internal/app/config_test.go`                                                   | G, C    |
| `agentplane/session_dispatcher/internal/app/server.go`                                                        | G       |
| `agentplane/session_dispatcher/migrations/BUILD.bazel`                                                        | G       |
| `agentplane/session_dispatcher/migrations/dispatcher/00014_baseline.sql`                                      | G       |
| `agentplane/session_dispatcher/migrations/migrations.go`                                                      | G       |
| `agentplane/session_dispatcher/migrations/test_migrations.py`                                                 | G       |
| `agentplane/session_dispatcher/migrations/upgrade_integration_test.go`                                        | G       |
| `agentplane/session_dispatcher/settings.py`                                                                   | G, C    |
| `agentplane/session_dispatcher/test_gateway_continuity.py`                                                    | G, C    |
| `agentplane/session_dispatcher/test_settings.py`                                                              | G, C    |
| `agentplane/session_dispatcher/testing/BUILD.bazel`                                                           | G       |
| `agentplane/session_dispatcher/testing/gateway_process.py`                                                    | G, C    |
| `agentplane/session_instructions/platform_instructions.j2`                                                    | G       |
| `agentplane/system_tests/README.md`                                                                           | G, C    |
| `agentplane/system_tests/debug/idle_resume_precondition.md`                                                   | C       |
| `agentplane/system_tests/debug/vm_guest_progress_and_host_crashes_2026_11_12.md`                              | C       |
| `agentplane/system_tests/debug/vm_native_startup_tracing.md`                                                  | G       |
| `agentplane/system_tests/debug/vm_shutdown_stalls/five_run_full_suite_rcu.md`                                 | G       |
| `agentplane/system_tests/debug/vm_shutdown_stalls/guest_only_reproduction.md`                                 | C       |
| `agentplane/system_tests/debug/vm_shutdown_stalls/rbe_200_flakiness.md`                                       | G       |
| `agentplane/system_tests/debug/vm_shutdown_stalls/standalone_kubevirt/README.md`                              | C       |
| `agentplane/system_tests/debug/vm_shutdown_stalls/standalone_kubevirt/host_instrumented_policy_off/README.md` | C       |
| `agentplane/system_tests/debug/vm_shutdown_stalls/standalone_kubevirt/proof_progress.md`                      | C       |
| `agentplane/system_tests/debug/vm_shutdown_stalls/virtiofs_tag_limit.md`                                      | C       |
| `agentplane/system_tests/deployment/BUILD.bazel`                                                              | G, C    |
| `agentplane/system_tests/deployment/README.md`                                                                | G, C    |
| `agentplane/system_tests/deployment/debug/fixture_ca_reuse.md`                                                | G       |
| `agentplane/system_tests/deployment/fixture_chart.py`                                                         | G, C, R |
| `agentplane/system_tests/deployment/test_render.py`                                                           | G, C, R |
| `agentplane/system_tests/driver/app_sandbox.py`                                                               | G, C    |
| `agentplane/system_tests/driver/central_sampler.py`                                                           | G, C    |
| `agentplane/system_tests/driver/environment_transport.py`                                                     | C       |
| `agentplane/system_tests/driver/ollama_warmup.py`                                                             | R       |
| `agentplane/system_tests/driver/sandbox_diagnostics.py`                                                       | C       |
| `agentplane/system_tests/driver/test_native_platform_clients.py`                                              | G       |
| `agentplane/system_tests/k3s/debug/process_trace_collection.md`                                               | G       |
| `agentplane/system_tests/k3s/debug/stack_timings.md`                                                          | C, R    |
| `agentplane/system_tests/k3s/debug/vm_shutdown.md`                                                            | G       |
| `agentplane/system_tests/k3s/defs.bzl`                                                                        | G, C, R |
| `agentplane/system_tests/k3s/test_pinned_images.py`                                                           | R       |
| `agentplane/system_tests/scenarios/central_sampler.py`                                                        | G, C    |
| `agentplane/system_tests/scenarios/dispatcher.py`                                                             | G, C    |
| `agentplane/system_tests/scenarios/egress_native.py`                                                          | R       |
| `agentplane/system_tests/scenarios/firecracker.py`                                                            | C       |
| `agentplane/system_tests/scenarios/runtime_proxy.py`                                                          | C       |
| `agentplane/system_tests/scenarios/session_deletion.py`                                                       | G       |
| `agentplane/system_tests/scenarios/storage.py`                                                                | C       |
| `agentplane/system_tests/scenarios/workload_trust.py`                                                         | G, C    |
| `agentplane/system_tests/suites/test_storage_startup.py`                                                      | C       |
| `agentplane/system_tests/vm/guest/guest_prepare`                                                              | G       |
| `agentplane/system_tests/vm/guest_logs.py`                                                                    | R       |
| `agentplane/system_tests/vm/test_guest_logs.py`                                                               | R       |
| `agentplane/testing/native_gateway_fixtures.py`                                                               | C       |
| `agentplane/testing/native_service_fixtures.py`                                                               | C       |
| `agentplane/testing/tls.py`                                                                                   | G       |
| `agentplane/x/firecracker_spike/README.md`                                                                    | R       |
| `agentplane/x/firecracker_spike/demo_spike.py`                                                                | R       |
| `agentplane/x/firecracker_spike/prepare.sh`                                                                   | R       |
| `agentplane/x/native_contracts/test_quiesce_cutoff.py`                                                        | C       |
| `agentplane/x/runner_layering/conftest.py`                                                                    | G       |
| `cluster/cdk8s/agent_sandbox.md`                                                                              | C       |
| `cluster/cdk8s/agent_sandbox.py`                                                                              | C       |
| `cluster/cdk8s/agentplane/BUILD.bazel`                                                                        | G, C    |
| `cluster/cdk8s/agentplane/actions.py`                                                                         | C       |
| `cluster/cdk8s/agentplane/actions_staging_policies.py`                                                        | R       |
| `cluster/cdk8s/agentplane/app.py`                                                                             | G, C    |
| `cluster/cdk8s/agentplane/app_settings.py`                                                                    | G       |
| `cluster/cdk8s/agentplane/central_sampler_workload.py`                                                        | G, C    |
| `cluster/cdk8s/agentplane/chart.py`                                                                           | G, C, R |
| `cluster/cdk8s/agentplane/command_sandbox.py`                                                                 | R       |
| `cluster/cdk8s/agentplane/egress.py`                                                                          | G       |
| `cluster/cdk8s/agentplane/environment_guest.py`                                                               | C       |
| `cluster/cdk8s/agentplane/environment_sandbox.py`                                                             | C, R    |
| `cluster/cdk8s/agentplane/environment_service.py`                                                             | C       |
| `cluster/cdk8s/agentplane/environment_vm.py`                                                                  | C       |
| `cluster/cdk8s/agentplane/launcher_relay.py`                                                                  | R       |
| `cluster/cdk8s/agentplane/notifications.py`                                                                   | G, C    |
| `cluster/cdk8s/agentplane/platform_routes.py`                                                                 | C, R    |
| `cluster/cdk8s/agentplane/runner_gateway.py`                                                                  | G       |
| `cluster/cdk8s/agentplane/runner_gateway_workload.py`                                                         | G, C    |
| `cluster/cdk8s/agentplane/sandbox_controller.py`                                                              | G, C    |
| `cluster/cdk8s/agentplane/sandbox_pod.py`                                                                     | C, R    |
| `cluster/cdk8s/agentplane/session_dispatcher.py`                                                              | G       |
| `cluster/cdk8s/agentplane/staging.py`                                                                         | G, C    |
| `cluster/cdk8s/agentplane/test_central_sampler.py`                                                            | G, C    |
| `cluster/cdk8s/agentplane/test_egress.py`                                                                     | G, C    |
| `cluster/cdk8s/agentplane/test_environment_firecracker.py`                                                    | R       |
| `cluster/cdk8s/agentplane/test_launcher_relay.py`                                                             | R       |
| `cluster/cdk8s/agentplane/test_notifications.py`                                                              | G       |
| `cluster/cdk8s/agentplane/test_runner_gateway_workload.py`                                                    | G, C    |
| `cluster/cdk8s/agentplane/test_sandbox_controller.py`                                                         | G, C    |
| `cluster/cdk8s/agentplane/test_sandbox_templates.py`                                                          | R       |
| `cluster/cdk8s/agentplane/test_service_boundaries.py`                                                         | G, C    |
| `cluster/cdk8s/agentplane/test_session_dispatcher.py`                                                         | G       |
| `cluster/cdk8s/agentplane/test_workload_certificate_admission.py`                                             | C       |
| `cluster/cdk8s/agentplane/test_workload_trust.py`                                                             | G, C, R |
| `cluster/cdk8s/agentplane/workload_routes.py`                                                                 | G       |
| `cluster/cdk8s/forgejo_registry/image_automation.py`                                                          | G, C, R |
| `cluster/cdk8s/generate_manifests.py`                                                                         | C       |
| `cluster/cdk8s/haku/namespace.py`                                                                             | R       |
| `cluster/cdk8s/haku/rbac.py`                                                                                  | R       |
| `cluster/cdk8s/haku/ui_egress.py`                                                                             | R       |
| `cluster/cdk8s/haku_ci/egress_gateway.py`                                                                     | R       |
| `cluster/cdk8s/monitoring/alloy.py`                                                                           | R       |
| `cluster/cdk8s/public_coder/egress.py`                                                                        | R       |
| `cluster/cdk8s/public_coder/egress_proxy_url.py`                                                              | R       |
| `cluster/cdk8s/public_coder/test_app.py`                                                                      | R       |
| `cluster/cdk8s/test_agent_rbac.py`                                                                            | G, C    |
| `cluster/cdk8s/test_generate_manifests.py`                                                                    | C       |
| `cluster/docs/decisions.md`                                                                                   | C       |
| `cluster/generated/agentplane-staging-delivery/agentplane-staging-delivery.k8s.yaml`                          | G, C    |
| `cluster/generated/agents/agent-sandbox/controller/patches.k8s.yaml`                                          | C       |
| `cluster/generated/artifact-generators/artifact-generators.k8s.yaml`                                          | C       |
| `cluster/generated/flux-image-automation-forgejo/flux-image-automation-forgejo.k8s.yaml`                      | G, C, R |
| `cluster/k8s/agentplane-staging/agentplane-staging.k8s.yaml`                                                  | G, C, R |
| `cluster/k8s/agentplane-staging/image-pins/kustomization.yaml`                                                | G, C, R |
| `cluster/k8s/agents/agent-sandbox/README.md`                                                                  | C       |
| `cluster/k8s/flux/kustomizations.k8s.yaml`                                                                    | C       |
| `cluster/k8s/forgejo-images/forgejo-images.k8s.yaml`                                                          | G, C, R |
| `cluster/parked/public-coder-agent/app/app.k8s.yaml`                                                          | R       |
| `cluster/parked/public-coder-agent/app/image-pins/kustomization.yaml`                                         | R       |
| `cluster/validation/crd_layering.py`                                                                          | C       |
| `cluster/validation/test_dependencies.py`                                                                     | C       |
| `cluster/validation/test_kustomize.py`                                                                        | R       |
| `devinfra/buildbuddy/debug/2026_10_30_publication_pnpm_abort.md`                                              | G       |
| `devinfra/ci/image_targets.json`                                                                              | G, C, R |
| `devinfra/debug/oci_loader_benchmark/ci_wide_fixture_logs.tsv`                                                | G       |
| `haku/docs/security.md`                                                                                       | R       |
