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

## Generated-client transport decomposition (2026-11-04 UTC)

This preceding launcher implementation used the generated Python `GuestManagement` stub directly over a private Unix
socket. Its small Go bridge owned no protobuf messages: it used the separately reviewed
[`util/vsock/firecracker`](../../util/vsock/firecracker/README.md) stream transport from PR #14136.
Agentplane owns port 52, private socket permissions, boot-ID checks and credential policy. The guest
uses `github.com/mdlayher/vsock` for its native listener and keeps host-CID authorization in the
management service. The protobuf-over-stdin/stdout helper and the hand-written native socket
implementation are removed.

[Transport and CLI tests](https://app.buildbuddy.io/invocation/ec134264-5bb7-4e83-829a-7db66712277c),
[generated Python-client integration and direct library typing](https://app.buildbuddy.io/invocation/20423d7c-fa57-4164-8595-8e6e575dd5aa),
and [rebased launcher/runner artifact builds](https://app.buildbuddy.io/invocation/11992c25-23f1-4db7-915f-3317e7239641)
passed. The Python test runs the actual Go bridge between generated gRPC client/server code,
covering a boot-time connection refusal, rejection before credentials are transmitted on a boot-ID
mismatch, deadline propagation, cancellation and transport cleanup. The OCI entrypoint/tooling test,
guest-management Go tests and supervisor tests also passed in
[the earlier refactor run](https://app.buildbuddy.io/invocation/80417da5-9a0d-4f18-b3bb-aa6bc2de1b9a);
that invocation as a whole failed on test runfiles/typing setup, repaired in the passing run above.

The rebuilt artifacts from code commit `74b7036981a53d616ec809d02bf65d0ea4871bcd` passed a new
manual KVM boot/suspend/restore check in a disposable Pod on `wyrm2`. The boot archive and extracted
seed rootfs were checked against the accepted digests above before starting. An older scratch
seed with hash `5dcc3a4f09a4ad419ba8c812acd6e7ca69323a4fd6c63d6cc9a33e9f8ca671b8` was rejected
by this provenance check; the accepted run used the immutable Nix archive without a guest patch.

Pod UID `c8989782-fd03-4631-ae8d-485e5d1c59dc`, guest boot ID
`0bedd7f2-e7b3-494f-9e2e-c5575774de43` and Runtime boot ID
`0179d3c6-f451-4c18-9b5e-283517ea3b6d` survived the checkpoint and restore. The manifest recorded
1 GiB RAM and generation `checkpoint-50d06bcf27ed414f8cb45c25f2667ad6`. The Runtime probe opened
native session `f23fce4c-ceba-4318-8837-293ea0966782`, then reattached with a higher connection epoch
and the previous output cursor. Reattachment returned the same UUID marker
`ef8755c6-c916-4cb2-aba5-5ead98d8e6a3`, shell PID 500 and child PID 501; the output counter advanced
from 0 to 2 and the process was neither closed nor exited. `StartProcess` was called only before
suspension. Reattachment was immediate, so the detached-session limitation described above remains.

While the guest was suspended, the host published a new synthetic projected-TLS generation signed
by the same CA. After restore, an authenticated TLS connection to Runtime returned the new leaf
certificate: SHA-256 `d4bf57e552937487870041b69877c76d3363e4ee31d93a50e51386090a666160`,
replacing `525d96274922b990dfd1be25fcafcae14a4585c88b2bd996b24d3753f7619cd0`. This checks the
current credential generation through generated Python gRPC, the reusable bridge and native guest
vsock, plus Runtime TLS reload without a Runtime restart. It does not test Kubernetes TokenReview
or projected ServiceAccount token rotation.

[Selected nonsecret events, manifest, certificate fingerprints and artifact digests](2026_11_03_firecracker_environment/grpc_bridge_evidence.json)
are retained. The proof still uses manually supplied launcher inputs and a synthetic Gateway-protocol
client, not a deployed Sandbox controller/Gateway/Session test. The disposable namespace was deleted
after saving evidence; no VM disks, RAM snapshots or private keys are retained in Git.

## Go supervisor and direct vsock gRPC (2026-11-04 UTC)

The current launcher is Go. Its generated `GuestManagement` client uses
`grpc.WithContextDialer` and the independent `util/vsock/firecracker.Dial` library directly.
The Python launcher runtime, intermediate bridge executable and private listening socket are
removed. The transport PR retains only the stream dialer. Firecracker's own control API remains
HTTP over its Unix socket.

[The integrated Go launcher and guest tests](https://app.buildbuddy.io/invocation/898bc023-0052-4d62-a1ed-50a66e0840cb)
passed. [The broader run](https://app.buildbuddy.io/invocation/1ea13874-fe40-4b3c-ad8f-a8cce774f4ae)
passed the actual launcher OCI startup, guest/transport and chart checks, but found a helper-process
test readiness race: a file existed before the helper wrote its arguments. After waiting for the
file's contents, [the full launcher Go suite passed](https://app.buildbuddy.io/invocation/1c144d69-65df-47ee-b23c-0e493b194ca1).
These tests cover coherent projected-Secret reads, direct gRPC startup, deadlines/cancellation,
identity checks before transmitting keys, no Synchronize replay, network guard ordering,
conservative workspace initialization, checkpoint integrity and VMM exit ordering.

The [launcher/probe artifacts](https://app.buildbuddy.io/invocation/10bdc42c-f798-47a7-8b61-b37096d79481)
were built from `4303939f11c1a7397d3f9541809290fac4515bf4`, above transport commit
`b77131876dd241d252365387195ddc936e66b33a`. The later readiness change is test-only.
The unmodified Go launcher OCI root and the previously verified immutable guest/runner artifacts
ran in a fresh disposable Pod on wyrm2. An initial chroot-harness launch could not open its log
because the host-side log directory was missing; no launcher or VMM had started. After verifying
there was no active marker or VMM, the harness created the directory and started the launcher.
This was a harness setup error; no uncertain VM start or native StartProcess was replayed.

The Pod UID remained `1c7900b6-6d72-4b87-8304-864cc47538f9`. The guest boot ID
`5b0b1d6c-0f02-4b0a-9d4f-00b53657aeab` and Runtime boot ID
`b4053167-114b-4ff6-a747-0213759329f1` survived full checkpoint/restore. Checkpoint
`checkpoint-9a04bf25-06cd-49a4-ba54-f106dc9677a7` contains 1,073,741,824 bytes of RAM.
The native execution session stayed `1b0c530d-5dd6-4125-95d1-f152768d9343`, with marker
`7c4770a8-184d-49ba-abc4-7e20dd238cdb`, shell PID 486 and child PID 487. Its counter advanced
from 0 to 1 after restore, then to 6 after a five-second observation and another cursor-based
reattachment. Output wall timestamps exceed the host's thaw-request timestamp, proving new
post-resume execution rather than only buffered pre-freeze output. The probe called StartProcess
once; both resumes required the existing session ID and output cursor.

While suspended, a complete synthetic projected-Secret generation replaced the Runtime leaf
certificate. After restoring RAM, the direct vsock gRPC synchronization refreshed credentials
before network admission. Runtime served the expected new certificate fingerprint
`2d30c945b278d05cae78117447ce4dfdf8ab254741886a838bf8a4d43f7dd302`, replacing
`391d0d47130b27e8d7d5874f4c0845f9f899f681627808e003c12ddaf80d79e5`.

[Selected events, manifests, thaw acknowledgements and artifact hashes](2026_11_03_firecracker_environment/go_supervisor_evidence.json)
are retained without credentials, RAM or disk contents. This repeats the manual Runtime-protocol
RAM/TLS proof; it does not test a deployed Sandbox controller/Gateway/Session/harness, projected
ServiceAccount-token rotation, the real egress proxy, k3s, cross-Pod recovery or a jailer. The Go
firewall policy has rule/order tests and the running guest accepted real mTLS Runtime ingress;
this run did not repeat the primitive spike's external proxy/direct-egress experiments. The
backend remains disabled in staging. The disposable proof namespace was deleted and verified absent.
