# Firecracker environment implementation validation

This implementation follows the [primitive spike](2026_11_02_firecracker_spike.md). The backend
contract is [Firecracker Sandboxes](../docs/firecracker_sandboxes.md). Source work started from
`origin/devel` at `e6cac7bf9cbc9ee428ef3487a4af4ddab17e2a0f`, then rebased onto `06c5ecc6aa`.
The boot/gRPC fixes are in `e87d03bef729cdf3633b73aef38d084afd0538d4`.

## Completed source checks

- [Controller, command, CRD admission, Gateway and environment-view tests](https://app.buildbuddy.io/invocation/8fb70a61-52a9-44c7-90c4-130302b044b9):
  all six targets passed. Admission tests exercise every template-arm combination and storage
  layout constraints. Controller tests retain the Pod and credentials, skip Quiesce during RAM
  suspension, hold missing launchers, fence stale supervisor UIDs and ignore pre-resume idle facts.
- [Firecracker chart and direct library lint aspects](https://app.buildbuddy.io/invocation/324f7b8d-19bc-443d-b892-bd01a30f1bee):
  passed. The template test checks relay-only projected tokens, explicit identities, native sidecar
  startup, privileged launcher, Runtime config and the read-only status network policy.
- [Existing Pod/KubeVirt template and Pod-policy regressions](https://app.buildbuddy.io/invocation/873096e1-eaa8-49de-9839-13087fed8c24):
  all four tests passed.
- [Launcher credential, storage, lifecycle-ordering and firewall tests](https://app.buildbuddy.io/invocation/6d25b761-94d7-4257-8f57-406900a11e73):
  all four targets passed, including conservative block-device initialization.
- [Updated gRPC launcher, direct Python libraries and OCI startup test](https://app.buildbuddy.io/invocation/30fe5e50-4190-4323-8d57-cb089996b52b):
  passed, including Ruff and mypy aspects. The RBE Docker test exercises the shipped entrypoint,
  pinned Firecracker 1.17.0, gRPC management CLI and disk/network tools.
- [Launcher OCI archive](https://app.buildbuddy.io/invocation/71e5505e-ff8e-4cad-8c16-ba57124a813f):
  built successfully and was used by the live proof below.
- [mTLS/native-execution Runtime probe](https://app.buildbuddy.io/invocation/30fe5e50-4190-4323-8d57-cb089996b52b):
  built successfully. WatchState stays open across the execution proof; bounded setup does not
  expire its connection epoch. Resume requires the existing session ID and output cursor and
  never falls back to starting a replacement process.
- [Guest-management server/client and runner EROFS extraction tests](https://app.buildbuddy.io/invocation/9f38b36d-f7cc-493f-a249-c4ceaf7b7893):
  all three targets passed.

Both NixOS configurations evaluate, and the Firecracker kernel and corrected OCI guest build
completed. The rootfs build checks writable NixOS system directories with `debugfs`; the thaw
service has `CAP_SYS_TIME` and `CAP_NET_BIND_SERVICE` for privileged vsock port 52. Source
checks and image construction are separate from the live boot/resume evidence below.

## Deployment boundary

The backend is disabled in staging. The implementation retains its launcher Pod across suspend;
Kubernetes scheduling reservations remain allocated. Missing/failed launchers are held for recovery,
not replaced or automatically cold booted. There is no cross-node restore or jailer in this change.

The live proof used an isolated `firecracker-environment-proof` namespace on the existing `wyrm2`
KVM node, with synthetic CA/Runtime/Gateway credentials. The Pod had no ServiceAccount token or
host network/PID namespace. It ran the extracted launcher OCI root with manually supplied config,
intent files and disks on Pod-local storage. This is the node's native Kubernetes/containerd stack,
not a new k3s cluster. It does not exercise deployed Sandbox-controller PVC/image-volume injection,
Gateway, Policy Controller, egress TokenReview or application Session/harness reconciliation.

## Disposable live KVM acceptance (2026-11-03 UTC)

The unmodified launcher and guest OCI artifacts built from this PR booted on `wyrm2` in the isolated
`firecracker-environment-proof` namespace. The proof Pod UID remained
`f652aa59-be0a-4fba-9998-6dd30231099e` throughout. Its launcher reported `running` only after
the guest's typed gRPC thaw acknowledgement. A synthetic mTLS Gateway probe called Runtime
`WatchState` and `GetStatus` successfully. The guest boot ID was
`ace8a6c7-96f4-4b0a-81ba-d8dc8ffde14a`; the Runtime process boot ID was
`cbcea396-2afc-4b89-8a31-9d334eff73c0`.

The probe then opened native execution session `3311c4f1-f621-469d-a993-adece39a7b6c` and
started one long-lived shell/child process. Its first output had marker
`425e1813-7d35-4b2e-8dfb-848e2a66d5c4`, counter 0, shell PID 500 and child PID 501. With the
same launcher Pod alive, setting the intent to `false` produced checkpoint
`checkpoint-7107a660b61a44cf9bfe69966c963875`. The launcher reached `suspended` only after a
durable manifest was written and the Firecracker VMM had exited. The manifest recorded the Pod and
guest boot IDs above and a 1,073,741,824-byte RAM snapshot. Setting intent to `true` restored the
same guest boot ID. Runtime connection epoch 3 reattached to the existing execution session using
its output cursor, **without another StartProcess**: the marker and both PIDs were unchanged, the
counter had advanced to 13, and the process was still open.

A second `false` intent produced durable generation
`checkpoint-a89ed3967c11417d8be31e0f3d38d484`, again with the same Pod/guest IDs and no VMM
at `suspended`. The second restore reached `running` with the same gRPC thaw boot ID. Runtime
connection epoch 4 reattached to the _same_ execution session from cursor 14; the marker, shell
PID 500 and child PID 501 were unchanged, the counter advanced to 34, and the process remained
open. The Runtime boot ID was unchanged in epochs 1 through 4. The second suspend request was
observed before 02:06:21 UTC and restore reached `running` at 02:07:20 UTC, so the checkpoint and
restore interval exceeded 30 seconds. This exercises process RAM preservation through two real
KVM snapshots and reconnects, not merely VM or Runtime restart identity.

The accepted inputs were the corrected guest OCI archive SHA-256
`a0334c39979b4766cdd8276ca50c7d6ca4cafac9bf5071099b0493a5e6e872e0` (the extracted
regular `rootfs.ext4` SHA-256 was `3e1b5562798adade36ea575a33c23051b8e8257f4312cb2b7357b7dd27e232e3`),
runner EROFS SHA-256 `df98b569fdcc3d32719df5c6eb16bc3fccfce9172ddddf954c6b6ef4d1459371`,
launcher OCI tar SHA-256 `65597a6e4cffded348a5d7de84279437b29bed60966cea633a7a88e076c17a48`,
and Runtime probe SHA-256 `49571ea6b450f9d0bc9fcca9726e785454059a1467fc5127c9f642e74a8dc29a`.
The launcher image's entrypoint `--help`, Firecracker 1.17.0, Firecracker guest-management CLI,
and disk/network tools were checked inside the extracted OCI root without manual tool symlinks or
environment overrides. The [launcher OCI build](https://app.buildbuddy.io/invocation/71e5505e-ff8e-4cad-8c16-ba57124a813f),
[launcher/probe tests](https://app.buildbuddy.io/invocation/30fe5e50-4190-4323-8d57-cb089996b52b),
and [guest/runner tests](https://app.buildbuddy.io/invocation/9f38b36d-f7cc-493f-a249-c4ceaf7b7893)
passed. [Selected probe events, thaw acknowledgements and the final manifest](2026_11_03_firecracker_environment/evidence.json)
are retained in Git. The complete focused logs were saved in task scratch `live-final-proof/`;
no keys, disks or RAM snapshots are in Git.

Earlier diagnosis used a separately patched copy of an older guest rootfs to find an immutable
`/etc` symlink and missing `CAP_NET_BIND_SERVICE` for privileged vsock port 52. That copy proved
two RAM restores and stable Runtime boot identity, but its older runner rejected the native
execution destination. The accepted result above uses the corrected guest/runner and
typed gRPC launcher, with neither rootfs patch. The final proof did not rotate credentials while
asleep or test a deployed Gateway, Policy Controller, egress TokenReview, provider traffic, or an
application Session/harness. Native execution-session reattachment was immediate after each
restore; the existing detached-session grace is about 30 seconds of _running guest time_, so this
does not establish arbitrary delayed reattachment after resume. No production resources were
changed. After saving the focused evidence, the disposable
`firecracker-environment-proof` namespace was deleted and verified absent.
