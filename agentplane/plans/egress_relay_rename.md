# Egress relay rename

**Deferred pending operator approval.** The operator requested the relay rename on 2026-10-16.
The proposal uses `relay` for the binary,
container and source role, image `agentplane-egress-relay`, bootstrap
`AGENTPLANE_EGRESS_RELAY_CONFIG_FILE` and `/etc/agentplane-egress-relay/settings.yaml`.
This is a local relay to the existing central egress proxy; do not rename that proxy, its
`spiffe://agentplane/egress-proxy`, Sandbox workload IDs or Identity resources. No new Service/SVID
is needed just because a sidecar becomes a relay. Keep already accurate `launcher-relay`,
`haku-ci-egress`, their DNS, and all proxy/listener ports and addresses.

R0 starts from these source and deployment owners.

- Binary/entrypoint and shared Go/Python settings: `agentplane/egress/BUILD.bazel`,
  `agentplane/egress/sidecar/{BUILD.bazel,cmd/BUILD.bazel,internal/relay/config.go}`,
  `agentplane/egress/sidecar_config.py`, `agentplane/egress/testing/sidecar_process.py`.
  Existing binary `/usr/local/bin/egress-sidecar`, container `egress-sidecar`, bootstrap
  `AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE`, settings path `/etc/agentplane-egress-sidecar/settings.yaml`.
- Publication/pins: `devinfra/ci/image_targets.json`, Forgejo image automation and Receiver, and the
  staging image-pins Component name `agentplane-egress-sidecar`; `agentplane/system_tests/k3s/defs.bzl` supplies its fixture image.
- Templates/injection: `cluster/cdk8s/agentplane/{sandbox_pod,command_sandbox,environment_sandbox,launcher_relay}.py`,
  `agentplane/environments/internal/controller/environment_runtime_credential.go`,
  `cluster/cdk8s/haku_ci/egress_gateway.py`. Root YAML contains ConfigMaps
  `environment-tools-egress-sidecar`, `launcher-relay-egress-sidecar`, `sandbox-egress-sidecar`,
  SandboxTemplate `environment-tools`, MutatingPolicy and ValidatingPolicy `agentplane-staging-launcher-relay`,
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
  Haku/mitmproxy components on a substring match. The Firecracker spike's artifact checksums record
  historical names. `cluster/cdk8s/monitoring/alloy.py` has prose about
  sidecars; telemetry source `sandbox_pod.py` uses `agentplane-egress-sidecar`, and
  `agentplane/egress/sidecar/internal/relay/runtime.go` initializes tracing as `egress-sidecar`.

On 2026-11-18 three running standing command boxes used the current relay image and
configuration. Refresh the inventory at R0. Relay routes load at process startup. ConfigMap
directory mounts can update on disk without changing the routes used by the running relay.
Preserve both launch-time routes and the settings a live or suspended workload would read on
restart until R4 verifies the last caller.

| Step | Running-work flag and ordered change                                                                                                                                                                                                                                                                                                                                            | Verification                                                                                                                                                                                                                                                            | Rollback                                                                                                                                                 |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R0   | **BREAKS RUNNING WORK: NO.** Accept relay scope; inventory live Pod/init-container/launcher and standing relay users, old image digests and checkpointed guests.                                                                                                                                                                                                                | Assign every image/settings/container-name reader and record the old manifest/pin.                                                                                                                                                                                      | No mutation.                                                                                                                                             |
| R1   | **BREAKS RUNNING WORK: YES.** Publish new image/automation first; deploy config readers accepting old/new bootstrap names and injector/controller readers recognizing old/new container names, still producing old templates. Preserve exact credential isolation.                                                                                                              | Old/new fake configurations start; credential injection covers regular and native-sidecar containers and still excludes user containers; image is pullable and reader image is pinned.                                                                                  | Old templates/config remain usable: restore reader pins after checking no new container/config is in use.                                                |
| R2   | **BREAKS RUNNING WORK: YES (launcher and credential injection).** Add new ConfigMaps/paths, then switch future Pod/command-box templates, KubeVirt injection and Firecracker launcher wiring with all callers in one coherent source change. Apply the approved `relay` container name only after R1 readers are running. Do not mutate existing Pods or guest relay addresses. | Render and system-test Pod/KubeVirt/Firecracker startup, readiness, proxy/3130 routes, credential mounts, native sidecar shutdown and suspend/resume. Check admission failure/UID fencing as well as success. Staging is the first test of Flux/Cilium/Kyverno objects. | Restore templates/injection and old ConfigMaps/pins; existing old and new containers remain understood. Verify new launch plus existing Session traffic. |
| R3   | **BREAKS RUNNING WORK: YES (standing relay restart and active proxy streams).** Roll `haku-ci-egress` and other confirmed standing consumers after new image/settings publication, maintaining their existing Service addresses, Identity grants and capacity. Move telemetry labels with dual queries; update docs and fixtures.                                               | Build/network requests, long-lived CONNECT/gRPC streams, all affected Identity authorization and both telemetry names work; verify actual controller account/container wiring.                                                                                          | Restore old image/config with R1 readers retained; verify Service endpoints and proxy requests. Do not rotate the standing Identity.                     |
| R4   | **BREAKS RUNNING WORK: YES.** Remove old ConfigMaps, image policies/pins and compatibility only after no live or suspended launch/relay, rollback image or credential injector needs old names. Stop old publishers after consumers move; do not force-recreate Sandboxes.                                                                                                      | Census plus live/snapshot inventory proves last reader gone; credential injection/egress/telemetry remain healthy.                                                                                                                                                      | Restore old ConfigMaps, readers and image availability before reverting any template or caller. Otherwise hold cleanup.                                  |
