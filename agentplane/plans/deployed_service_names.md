# Retire legacy Sessions and Environments deployment names

S8 removes the `runner-gateway`, `session-dispatcher` and `sandbox-controller` Services,
the two stopped Deployments and their settings, service accounts, grants and Certificates,
the corresponding central CONNECT passthrough authorities and the old image automation
and bootstrap aliases. The [system overview](../docs/system_overview.md) owns current routing
and identities. The separate [egress relay rename](egress_relay_rename.md) is deferred.

## Pre-removal evidence

- On 2026-11-18, staging had zero Sandboxes, KubeVirt VMs, VMIs or snapshots and zero
  replicas of the legacy Deployments. An all-namespace ConfigMap URL census found the
  retired hosts only in central CONNECT passthrough and the stopped workloads' settings.
  The operator waived compatibility for platform URLs in stored Central Sampler guides.
- On 2026-11-18 at 13:44–13:45 UTC, disposable Pods on `wyrm2` pulled both canonical images
  with `imagePullPolicy: Always` and the staging pull Secret. The Pods lacked service settings
  and were removed after the pulls. This checked registry access by digest. The images were
  already serving in staging. Repositories are under `git.allegedly.works/ducktape-ci/`.

  | Image                     | Tag                            | Pulled digest                                                             |
  | ------------------------- | ------------------------------ | ------------------------------------------------------------------------- |
  | `agentplane-sessions`     | `devel-20261118102629-c2927c5` | `sha256:708a73f64476b75aa87cf08488093d1980b0c39f36f84c5a34d65a542b463e32` |
  | `agentplane-environments` | `devel-20261118102633-c2927c5` | `sha256:e45dfa2f6ecfcb9eeaa77076535f8b012b7d16ac4e4ccb80be2a81044cff8248` |

- A fresh Pod Sandbox passed both managed CLI RPCs after [the relay target cleanup](https://github.com/agentydragon/ducktape/pull/17623).

## Close after staging reconciles

1. Confirm the Agentplane root, delivery, Forgejo image automation and image Receiver Flux
   owners applied the retirement. Check that both canonical Deployments and Service
   EndpointSlices are ready, the Environments leader Lease renews and metrics retain a single scrape.
2. Confirm the three legacy Services and their EndpointSlices, stopped workloads and their
   resources, old CONNECT authorities and image automation entries are gone. Check both
   managed CLI RPCs from a fresh Pod Sandbox and scan affected workloads for new TLS,
   authorization or routing errors. Remove this checklist and its DAG and index entries
   after these checks pass.

## Rollback

Revert the S8 PR and wait for Flux to restore the Services, CONNECT entries, settings,
accounts, grants, certificates and image automation. The restored Services should route to
the canonical Deployments through the shared selector. Confirm both endpoints and SVID-authenticated
RPCs before restoring any retired caller or scaling up an old Deployment. If a legacy reader must
be restored, verify the restored pin can pull and the workload accepts its config before enabling
it. Keep the live PostgreSQL contents, retained identities and workspace state through the revert.
