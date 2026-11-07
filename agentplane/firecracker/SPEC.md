# Firecracker launcher contract

The `environment-runtime` container owns one Firecracker VM for the lifetime of its Sandbox Pod.
The Sandbox controller writes the Pod annotation projected at
`/run/agentplane/firecracker-intent/running`; `true` requests a running VM, and `false` requests a
full RAM checkpoint. The launcher never changes that annotation. It serves read-only `GET /state`
and `GET /readyz` on Pod port `9092`. Readiness requires the `running` phase and a reachable guest
Runtime on port `9090`.
Once suspension is requested, resume or deletion waits for a matching supervisor's `suspended`
acknowledgement before requesting a running VM. This ensures the projected intent cannot skip a
checkpoint while deletion waits for the post-restore Gateway connection epoch.

The launcher reads strict JSON configuration from `/run/agentplane/firecracker/config.json`.
Pod environment supplies `POD_UID`, `SANDBOX_UID`, `FIRECRACKER_VCPU_COUNT`,
`FIRECRACKER_MEMORY_MIB`, `FIRECRACKER_BOOT_IMAGE`, `FIRECRACKER_RUNNER_IMAGE`, and `WORKSPACE_KIND`.
The image values identify immutable OCI inputs; the mounted files are under `/images/boot` and
`/images/runner`. The optional JSON settings are `relay_port`, `platform_route_ports`,
`state_size_mib`, and `workspace_size_mib`. The latter two default to `2048` and `8192` MiB.
The boot image supplies `vmlinux`, `rootfs.ext4`, and an `init` file naming one absolute guest
init path. An optional `initramfs.cpio` loads modular boot drivers before starting that init.
When present, the launcher stages the initramfs and includes it in checkpoint verification
and restoration along with the kernel and disks.

`/var/lib/agentplane/state/firecracker/owner.lock` is held with `flock` across boot, checkpoint,
and resume. A full checkpoint records VM state, guest RAM, and every backing disk at the same
paused boundary, then atomically publishes a manifest with Pod/Sandbox identity, configuration
and VMM digests, and the complete file set and sizes. The marker `active` is durable before any VM
continuation. A launcher crash while it exists is an uncertain continuation and fails closed on
restart. A different Pod or changed
VMM cannot resume a checkpoint. A failed new checkpoint leaves the prior manifest intact, but
the uncertain active marker blocks automatic retry. The current checkpoint remains on the state
PVC across same-Pod suspension; its predecessor is removed after the next VMM exits.
The guest thaw acknowledgement must carry the checkpoint's boot ID before network admission.
A same-Pod restore checks that every generation file is regular and has its recorded size before
restoring any backing disk. It always restores disks from the committed generation: regular files
are copied, synced and atomically replaced; block workspaces are copied and synced. It does not
compare or reuse existing live disk contents. RAM, VMM state and disk contents are not checksummed;
same-sized corruption is outside this local checkpoint contract. The paused capture, owner lock,
active-continuation fence and durable manifest publication establish consistency.
VMM control requests have a 20-second deadline. Snapshot create/load requests have a separate
two-minute deadline for RAM file I/O and synchronization; an expired request is never retried.
For a block workspace, an existing ext4 filesystem is preserved. An unsigned all-zero device is
formatted as ext4 only after scanning its full contents; another signature, nonzero data, or an
inspection error stops boot before the VM starts.

The TAP uses `192.0.2.1/30` and `fd00:fc::1/64` at MTU `1280`. The guest can reach only the
declared relay and platform route TCP listeners on the host. Forwarding allows established
replies to inbound Runtime traffic on port `9090` and rejects guest-initiated off-Pod flows.
Firewall changes install parent-chain reject guards before modifying private chains. Guest
network admission follows the typed `GuestManagement.Synchronize` response over AF_VSOCK
port `52`. The host first reads `GetStatus`, checks the expected boot ID, and starts credential
delivery. `Synchronize` repairs wall time and waits for a fresh Workload API subscription before
acknowledging the same boot ID. Transport establishment has a two-minute boot budget; the single
synchronization RPC has its own 30-second deadline and is never replayed automatically.

The launcher serves the standard SPIFFE Workload API X.509 profile from one coherent projected
Secret generation. It polls for changes every second and streams complete replacements. Issuance
remains owned by cert-manager; JWT issuance is disabled. The first valid Runtime SPIFFE ID is pinned
for the continuation, and changed identities, invalid chains and mismatched keys are rejected.
An unavailable or invalid projection is logged and retried; the last accepted credential remains
usable only until its certificate expires. No invalid update replaces accepted state.

Both vsock connections are initiated by the host through Firecracker's Unix socket. Port `52`
serves guest-management gRPC; port `53` accepts a byte stream on which the guest is the Workload API
client and the host is its gRPC server. The root guest service accepts only host CID `2` and holds
`CAP_SYS_TIME` and `CAP_NET_BIND_SERVICE`. Binding privileged ports prevents unprivileged guest
programs from impersonating either listener to receive private keys. There is no guest-accessible
host credential listener, intermediate binary or TCP management endpoint.

The guest agent mirrors validated updates to a mode-0600 Unix socket in its root-only Runtime
credential directory. Runtime watches the stream with go-spiffe and retains pinned credential snapshots in
memory for subsequent control and History TLS handshakes. It does not write certificate/key files or restart on renewal.
The guest refreshes the host subscription on every thaw, even when its restored RAM contains an
old accepted credential. Networking stays fenced until the fresh update reaches the guest agent;
Runtime consumes that update asynchronously. Existing TLS connections are not reauthenticated by
credential renewal. The host credential service is stopped and joined before VM pause/snapshot.
The launcher pins Kubernetes Secret `..data` once per read so a projected generation change cannot
pair a new certificate with an old key. There is no credential seed disk.

The Pod template currently grants the launcher root and privileged access for KVM, TAP, image
construction, and firewall setup. The VMM uses its built-in seccomp filters, but this launcher
does not yet use Firecracker's jailer. The image and rule tests do not establish a KVM runtime
boot or an end-to-end isolated tenant boundary; those require the live Pod acceptance run.

Launcher configuration digests are domain-separated by implementation version. The size-record
manifest replaces the earlier experimental content-digest format; old manifests are rejected
rather than migrated. Replace the disposable proof Pod before using this format. This does not
enable Firecracker in staging.
