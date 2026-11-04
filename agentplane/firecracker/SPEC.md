# Firecracker launcher contract

The `environment-runtime` container owns one Firecracker VM for the lifetime of its Sandbox Pod.
The Sandbox controller writes the Pod annotation projected at
`/run/agentplane/firecracker-intent/running`; `true` requests a running VM, and `false` requests a
full RAM checkpoint. The launcher never changes that annotation. It serves read-only `GET /state`
and `GET /readyz` on Pod port `9092`. Readiness requires the `running` phase and a reachable guest
Runtime on port `9090`.

The launcher reads strict JSON configuration from `/run/agentplane/firecracker/config.json`.
Pod environment supplies `POD_UID`, `SANDBOX_UID`, `FIRECRACKER_VCPU_COUNT`,
`FIRECRACKER_MEMORY_MIB`, `FIRECRACKER_BOOT_IMAGE`, `FIRECRACKER_RUNNER_IMAGE`, and `WORKSPACE_KIND`.
The image values identify immutable OCI inputs; the mounted files are under `/images/boot` and
`/images/runner`. The optional JSON settings are `relay_port`, `platform_route_ports`,
`state_size_mib`, and `workspace_size_mib`. The latter two default to `2048` and `8192` MiB.

`/var/lib/agentplane/state/firecracker/owner.lock` is held with `flock` across boot, checkpoint,
and resume. A full checkpoint records VM state, guest RAM, and every backing disk at the same
paused boundary, then atomically publishes a manifest with Pod, Sandbox, configuration, VMM, and
file digests. The marker `active` is durable before any VM continuation. A launcher crash while
it exists is an uncertain continuation and fails closed on restart. A different Pod or changed
VMM cannot resume a checkpoint. A failed new checkpoint leaves the prior manifest intact, but
the uncertain active marker blocks automatic retry. The current checkpoint remains on the state
PVC across same-Pod suspension; its predecessor is removed after the next VMM exits.
The guest thaw acknowledgement must carry the checkpoint's boot ID before network admission.
For a block workspace, an existing ext4 filesystem is preserved. An unsigned all-zero device is
formatted as ext4 only after scanning its full contents; another signature, nonzero data, or an
inspection error stops boot before the VM starts.

The TAP uses `192.0.2.1/30` and `fd00:fc::1/64` at MTU `1280`. The guest can reach only the
declared relay and platform route TCP listeners on the host. Forwarding allows established
replies to inbound Runtime traffic on port `9090` and rejects guest-initiated off-Pod flows.
Firewall changes install parent-chain reject guards before modifying private chains. Guest
network admission follows the typed `GuestManagement.Synchronize` gRPC response over AF_VSOCK
port `52`. The host first reads `GetStatus`, then sends the current clock, TLS certificate, key,
CA bundle and expected boot ID; the guest rejects a mismatched identity before changing state.
The host client retries transport establishment for up to two minutes while the guest boots.
After `GetStatus`, the single credential synchronization RPC has its own 30-second deadline.
The Go supervisor uses the generated gRPC client directly with a custom context dialer from
`agentplane/firecracker/vsock`. The dialer performs Firecracker's `CONNECT 52` handshake on the VMM's
Unix socket and returns the connection to gRPC. There is no intermediate process or private
listening socket. gRPC owns serialization, message limits, deadlines and cancellation; each
synchronization closes its client connection on success or failure.
The root guest service accepts only host CID `2` and holds `CAP_SYS_TIME` and
`CAP_NET_BIND_SERVICE`. Keeping its listener below port `1024` prevents an unprivileged guest
process from replacing the service and receiving Runtime private keys. It exposes no TCP listener.
The launcher resolves Kubernetes Secret `..data` once per credential read so a projected
generation change cannot pair a new certificate with an old key.

The Pod template currently grants the launcher root and privileged access for KVM, TAP, image
construction, and firewall setup. The VMM uses its built-in seccomp filters, but this launcher
does not yet use Firecracker's jailer. The image and rule tests do not establish a KVM runtime
boot or an end-to-end isolated tenant boundary; those require the live Pod acceptance run.

Launcher configuration digests are domain-separated by implementation version. Checkpoints from
the earlier experimental Python launcher are rejected; replace its disposable proof Pod before
using the Go launcher. This does not enable Firecracker in staging.
