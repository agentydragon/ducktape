# Deployed-name rollout: S0 rollback state

The operator released execution on 2026-11-17 at 00:03 UTC. Scope is deployed names
`sessions` / `environments`, image repositories `agentplane-sessions` /
`agentplane-environments`, and bootstrap prefixes `AGENTPLANE_SESSIONS_` /
`AGENTPLANE_ENVIRONMENTS_`. Every SPIFFE ID, YAML key, database name, Goose history,
Lease ID, SSA field manager, proto/wire name and stored string stays unchanged.
Server/observer identity changes and the egress relay rename are unreleased.

This is a non-atomic read-only baseline: agent reads at approximately 00:06–00:14 UTC,
supplemented by the coordinator's reads at 00:31–00:35 UTC. Repository inventory was
checked against fresh `origin/devel` at `0c9c862ac3e747dbbbf7e0686abd11095b8959a7`,
then rechecked at `3bbb34a6db4e12fdb099edf1184a37432d3ddd8b`. No staging resource was
restarted, resumed, recreated or deleted for these observations.

## Source and rendered ownership

- Root resources: `cluster/k8s/agentplane-staging/agentplane-staging.k8s.yaml`;
  Flux owner `ducktape-flux/agentplane-staging`, path `./cluster/k8s/agentplane-staging`.
  Root owns the stable Services and Environments workload.
- Sessions workload: `cluster/generated/agentplane-staging-delivery/agentplane-staging-delivery.k8s.yaml`;
  Flux owner `ducktape-flux/agentplane-staging-delivery`, path
  `./cluster/generated/agentplane-staging-delivery`.
- Both use `cluster/k8s/agentplane-staging/image-pins/kustomization.yaml`.
  Rendered `:unset` images must be resolved through this Component.
- `ducktape-flux/ducktape` GitRepository revision was
  `devel@sha1:0c9c862ac3e747dbbbf7e0686abd11095b8959a7`.
  Sparse checkout includes `cluster/{k8s,generated,parked}/`, `secrets/shared/`,
  `agentplane/crds/manifests/`, `cluster/charts/browsertrix/`, `props/deploy/`, `tf/gitops/`.
  No owner, source path or directory move is part of the rollout.
- Observed applied artifact revisions: root
  `latest@sha256:19a435de66d269b79f96d2daae38f441b64af7e0b73981add733fe23d90bf6ef`;
  delivery `latest@sha256:f08428b09538b3107b9f191da5bbfc39619efa67dcb189d2250624e65258f1f6`.
  Both reported Ready and Healthy.
- Image automation owner `ducktape-flux/flux-image-automation-forgejo`, path
  `./cluster/generated/flux-image-automation-forgejo`, reported Ready at
  `latest@sha256:d2b9ffb9886631dd6463483765b01f3cdd6c425617a4bb01cee24f8398191d16`.
  `ducktape-flux/forgejo-images`, path `./cluster/k8s/forgejo-images`, reported Ready at
  `latest@sha256:8c8b01b2aaef705429b86fc4b0f2d9697e80bfabb01bd648a28d35e59b6b2be2`.
- The fresh name census contains the plan's 348 paths plus the plan itself. Relative
  to its original inventory, Sessions wake admission additionally permits Actions;
  Environments retains its separate wake allowlist. Preserve that distinction.

## Supported rollback images and workloads

All repositories below are under `git.allegedly.works/ducktape-ci/` (Forgejo).
These are S0's supported rollback images, including after new repositories publish.

| Deployment           | Tag                                                          | Resolved running digest                                                   |
| -------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------- |
| `runner-gateway`     | `agentplane-runner-gateway:devel-20261117000154-daf1a09`     | `sha256:6f2e800f257f623b3888c0d1a26c59054e0e9b1e62b0a7807c9c86c665c20e52` |
| `sandbox-controller` | `agentplane-sandbox-controller:devel-20261116205728-96abe4d` | `sha256:05d92276646cf380ee3a0ed5fec592d80a5b5bdf29d4e946889574fd4c36a488` |

- `runner-gateway`: UID `29a928b2-7e9a-4f85-a354-c2c6901c6259`, generation and observed
  generation 348, 2/2 ready/updated/available. Ready Pods
  `runner-gateway-744bb67df6-{mkdwj,xk2g9}` have zero restarts.
- `sandbox-controller`: UID `af62b7b0-bce9-4017-b7db-784bec9bc00e`, generation and observed
  generation 160, 2/2 ready/updated/available. Ready Pods
  `sandbox-controller-64b8547fcb-{2fhhg,cnltp}` have zero restarts.
- After scale-down, retain old Deployments as scaled-to-zero rollback manifests with
  their pins until the coordinator certifies the live/suspended last-caller census for removal.
- Registry pullability: **not captured**. Anonymous reads return 401 and the agent has
  no authenticated registry route; nodes currently run these digests. The coordinator
  reports no installed package cleanup rule; `cluster/x/forgejo_retention_report` is
  report-only. Retain these tags and digests throughout rollback support.

## Services, bootstrap, credentials and Lease

| Service              | UID                                    | Selector                                                                   | Ready endpoints                                          |
| -------------------- | -------------------------------------- | -------------------------------------------------------------------------- | -------------------------------------------------------- |
| `runner-gateway`     | `9e8385f3-3e73-4ca5-b021-383fb9cfe177` | `app.kubernetes.io/name=runner-gateway`                                    | Both Gateway Pods, TCP `8080`                            |
| `session-dispatcher` | `723fe94c-f466-434f-977e-a51546619437` | `app.kubernetes.io/name=runner-gateway`                                    | Same two Pods, TCP `8080`                                |
| `sandbox-controller` | `8be7084e-b7e4-42d2-a065-ecd930f871d2` | `cdk8s.io/metadata.addr=agentplane-sandbox-controller-deployment-c8a54474` | Both controller Pods, TCP `8082` gRPC and `8080` metrics |

Sessions uses `AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE` at
`/etc/agentplane-runner-gateway/settings/settings.yaml`, plus
`AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL` from Secret
`postgres-session-dispatcher`, key `uri`. Environments uses
`AGENTPLANE_SANDBOX_CONTROLLER_CONFIG_FILE` at `/etc/agentplane-sandbox-controller/config.yaml`.
These live Pod inputs agree with the rendered manifests and loaders at the image tag commits.
Neither workload has a destination-audience token projection; serving admission is SVID-only.
No Secret contents were read.

All four Certificates were Ready under ClusterIssuer `agentplane-workload`, with 24-hour
duration and 8-hour renewal lead time:

| Certificate → Secret                                                                                        | URI                                      |
| ----------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| `runner-gateway-workload` → `runner-gateway-workload-tls`                                                   | `spiffe://agentplane/session-dispatcher` |
| `runner-gateway-sessions-client-workload` → `runner-gateway-sessions-client-workload-tls`                   | `spiffe://agentplane/sessions`           |
| `sandbox-controller-client-workload` → `sandbox-controller-client-workload-tls`                             | `spiffe://agentplane/sandbox-controller` |
| `sandbox-controller-runtime-proxy-client-workload` → `sandbox-controller-runtime-proxy-client-workload-tls` | `spiffe://agentplane/runtime-proxy`      |

Controller Lease `agentplane-sandbox-controller`, UID
`e9f2f259-76b7-4d97-a6a4-19d7337d12bc`, holder
`sandbox-controller-64b8547fcb-cnltp_6195dfbf-b72f-4872-a5d2-de9f4e4ee000`,
duration 15 seconds, was renewing (coordinator observed renewal at 00:33:59 UTC).
SSA managers remain `agentplane-sandbox-controller` and `agentplane-runner-gateway`.

## Sandboxes, retained callers and continuity

The coordinator certifies exactly two Agentplane Sandboxes, both Pod-backed
`environment-tools`, Running and Ready; none suspended or checkpointed:

| Sandbox             | Sandbox UID                            | Incarnation / Pod UID                  | Pod started            |
| ------------------- | -------------------------------------- | -------------------------------------- | ---------------------- |
| `codex-cleanup-tls` | `54d76deb-34a2-43af-914f-f5d99f36d732` | `bd239302-e7eb-416a-9824-08b412148f49` | `2026-11-14T08:25:01Z` |
| `codex-workers`     | `e1483d31-dfe8-4be9-89c9-c34ad2c50d96` | `727cd97e-0278-489e-a926-31b5b087649b` | `2026-11-16T20:34:12Z` |

Both run `environment-tools:devel-20261112103718-61a1079`, digest
`sha256:14439deb846398d8d721b4f8b644d75231e7e6f8389e5307d4a04fa65ab71c0f`.
Relay digests respectively are
`sha256:bdaf24688741c1c23eb4fdc058b5100636ea51316729651bc8a9a8ed93760b44` and
`sha256:95ba4498fa30f79390e5eed620480d762b1057ef369617e703f72da6dacd0e93`.
Each `runnerCredential` has kind `Pod` and is bound to its incarnation UID.
WorkloadCredential and Identity object details: **not captured**; neither the agent's
accounts nor the coordinator's read account can list them.

Retained state/workspace PVC UIDs respectively:

- `codex-cleanup-tls`: state `0a763299-4ec1-4b52-88e9-17869e635b9d`, workspace
  `f5420715-92be-42df-a189-323204d90ad2`.
- `codex-workers`: state `94da27dd-b2aa-4a84-b70f-5de5e1cf2978`, workspace
  `a30011f9-39f7-4b7d-8f20-3ba86791507d`.

All four PVCs are Bound and retain the Sandbox owner UID. Runtime journals and
workspaces remain on these PVCs. No contents were inspected or copied.

Current template tags are `environment-tools:devel-20261116203458-db68d2d` and
`agentplane-egress-sidecar:devel-20261116031927-926c288`; both live Runtime images
predate the template. No staging KubeVirt VM/VMI exists. The unused VM template pins
`agentplane-environment-vm:devel-20261116205856-385a26c` and
`agentplane-runner-disk:devel-20261116214313-6ea14dc`. No Firecracker template or Pod exists.

**Touches running work:** `environment-tools-egress-sidecar` is mounted as a directory,
without `subPath`; its changes propagate into running Pods' files. It and
`launcher-relay-egress-sidecar` contain upstreams
`runner-gateway.agentplane-staging.svc.cluster.local:8080` and
`sandbox-controller.agentplane-staging.svc.cluster.local:8082`, with the old controller
server URI. Add new routes alongside old ones; retain old upstreams until retirement.
Do not restart an existing Sandbox to pick up routes. Effective process configuration
must be distinguished from the current mounted file.
Source at the two live relay image commits (`9e1f8fe`, `926c288`) loads route settings
only during process startup and handles SIGINT/SIGTERM, with no route-config reload
path. The former's entrypoint is `agentplane/egress/sidecar/main.go`; the latter's is
`agentplane/egress/sidecar/internal/relay/runtime.go`. Mounted-file propagation does not
retarget an existing process. Retain old routes for its eventual restart as well.

`environment-tools-config` mounts only `environment-runtime.json` with `subPath`;
edits do not reach existing Pods. Its old-name matches are instructions, not targets.
Neither live Sandbox has an old-name service environment variable. Other old-name
ConfigMaps: `actions-settings`, `app-config`, `central-sampler-settings`,
`egress-go-settings`, `notifications-settings`, `runner-gateway-settings`,
`sandbox-controller-settings`. The sampler's `gateway_target` names the controller.

The coordinator reports healthy native Sessions on `codex-workers`; this Session is
one. Sampler `devel-20261116234934-53a0c08` is 2/2 ready. No new workload or resume
was triggered to establish this baseline. Live VM/Firecracker continuity: **not captured**,
because neither backend is in live use. Known VM-test faults: guest timer stall
(#16997) and QEMU iotlb assertion (#17034).

## Database, recovery and monitoring gaps

Dispatcher backlog, uncertain delivery counts, PostgreSQL delivery-lease holders and
Goose version: **not captured**. Neither read account has database access, and Dispatcher
exports no backlog metric. Database/owner `session_dispatcher`, Goose table
`runner_gateway_dispatcher_goose_version` and advisory lock `0x4453_5043` remain unchanged.
Per-Session PostgreSQL leases fence old/new Deployment delivery. The coordinator will
read backlog and lease holders before retirement; these gaps do not block stages S1–S5.

CNPG cluster `postgres`, UID `bb171b8a-6721-4749-99e1-fbcb7c7752df`, is Ready with
two instances and continuous archiving. It holds all databases. Daily ScheduledBackup
is 02:49 UTC using the barman-cloud plugin; backups `postgres-202611{14,15,16}024900`
completed around 03:03 UTC. PITR window: **not captured**; CNPG reports no
`firstRecoverabilityPoint`. Isolated restore verification: **not captured**; none is on
record. Consistent recovery proof across PostgreSQL and Runtime volumes: **not captured**.
The rename changes no data, schema, database owner or storage association. A rollback
must preserve post-cutover writes; it must not restore a stale backup over current work.

The coordinator found no old-name matches in PrometheusRule or GrafanaDashboard CRs.
Repository monitoring searches also found none. Cluster-wide dashboard ConfigMaps:
**not captured**, because the read account cannot list them. ServiceMonitor
`agentplane-staging/sandbox-controller` names the old Service. Keep its scrape on the
old Service until retirement, ensuring one scrape per endpoint during overlap.

The coordinator accepts the recorded gaps for names-only stages S1–S5 and personally
certifies the complete live/suspended/checkpointed last-caller census before S8 removal.
