# Runner control

The runner serves native gRPC on one private listener: `Runner`, `Executor` and standard gRPC
health. The Environments proxy derives its address from the current workload's Pod IP and the runner port;
KubeVirt forwards that port into the guest. Environments proxy and Controller binding-observer peers reach it with separate method authority. History Event
ingress remains a separate outbound platform route.

Runner configuration requires `control.listen_address` and `control.security.mode`; the Environments proxy
requires the matching `environment_service.runtime_proxy.runner_security.mode`. Each explicitly selects `mtls` or `insecure`. `mtls`
requires a `tls` block with absolute `cert_file`, `key_file` and `ca_file` paths, or a local
`workload_api_socket` Unix URI. A file-backed block may add `client_ca_file`, which then alone
verifies the client certificates this side serves; `ca_file` keeps verifying its own chain and the
servers it dials. Firecracker Runtime selects the Workload API; the proxy uses
projected files. Source forms cannot be mixed. `insecure` rejects a TLS block. Missing/unknown modes and partial or contradictory configuration fail startup.
The service definitions are `env/runtime/control/environment_runtime.proto` and `env/runtime/execution/executor.proto`;
`proto/environment_runtime_state.proto` defines the facts published by `Runner.WatchState`.

## State observation and fencing

The Controller serves EnvironmentService on every replica at `environments:8082`, using its
existing service SVID. This inventory/desired-state listener has no Runtime command client and does
not change the leader-only observation below. The optional Runtime proxy on the same listener
separately owns callable Runtime connections on every replica. Its readiness depends on
local serving and policy watches, independently of leadership. App, Actions and Sandbox CLIs call this receiver with their service or workload SVIDs. The Sessions binary
does not register EnvironmentService; automatic wake grants remain with the Dispatcher. See [the Controller contract](../environments/SPEC.md#environmentservice-listener).

The leader-elected Sandbox Controller independently opens `WatchState(observe_only=true)` directly
to each accepted incarnation, authenticating with its dedicated client SVID. This stream grants no
command or Executor authority. Its authenticated request names the
Sandbox UID and current workload Pod UID; for a VM, the incarnation is the virt-launcher Pod UID.
Runtime uses this watch to bind the incarnation for its process lifetime and completes ordinary
lifecycle adoption before returning the first snapshot. Each Runtime process has a fresh boot ID.
The Controller supplies the current workload UID to a new process, and reconnects may retry that
UID; neither the Controller nor a proxy may change the binding after adoption begins. The
Controller keeps bootstrap, quiesce, setup, idle and blocker facts in memory keyed by Sandbox UID,
incarnation UID and boot ID.
A local watch generation fences callbacks from replaced streams. A new leader relists Sandboxes
and starts fresh watches and idle timers. Lost streams retry after one second; a 30-second
heartbeat timeout clears idle eligibility immediately, while connectivity status keeps a one-minute
grace. Reconnects start a full idle timer, even when the Runtime reports the same idle timestamp.
Quiesce attempts remain persisted, and an unobserved published incarnation must still seal before
stop or deletion. Bootstrap failure remains terminal for its incarnation.

## Runner proxy

Each Environments replica maintains its own callable Runtime connection. See the
[proxy contract](../environments/runtimeproxy/SPEC.md) for observation, routing metadata and
service grants. Session clients use Dispatcher. The App uses
[EnvironmentService diagnostics](../environments/SPEC.md#live-runtime-diagnostics).

## Event ingress

The SandboxTemplate sets one `ingress.target` for every Session journal in its Sandbox. The
Runtime pushes to that endpoint independently of control RPCs. Deployments that keep History
use the [History ingress transport and writer binding](../history_service/README.md#runner-ingress).
A deployment can instead implement [`SessionJournalIngress.Push`](../history_service/api/ingress/session_ingress.proto)
with its configured authentication. Downstream fan-out belongs to the ingress.

The Runtime opens one stream per Session at startup, on creation, and after stream loss until
the seal is acknowledged. The streams share a connection, so one Session's catch-up does not
hold up another Session's delivery or commands. Every call starts with an `Attached` snapshot.
The ingress replies with the committed cursor, then the Runtime streams the remaining journal
in order and sends the seal after its last entry. History accepts exact replays, so a lost
acknowledgement can be recovered by reconnecting.

The [Runtime ingress client](../env/runtime/ingress/client.go) limits entries in flight and
checks acknowledgements against the local journal. It treats an initial acknowledgement beyond
the local tail as `FAILED_PRECONDITION`. Remote failures retry per Session through
[the shared stream backoff](../env/runtime/stream/retry.go), with the maximum delay for
`PERMISSION_DENIED` or `FAILED_PRECONDITION`. The Runtime persists the latest acknowledgement
and keeps the journal entries on its state volume. Unacknowledged lag is bounded by that volume.

`Quiesce` closes admission and starts draining and sealing the journals. `WatchState` reports
`quiesced` after ingress acknowledges every seal. Repeated `Quiesce` requests continue the same
operation. The Sandbox Controller retries an unfinished request at its one-minute deadline
and reconciles sooner on an observed stage change. The persisted request time survives a
Controller restart. Volume deletion waits for acknowledged seals. A Runtime that cannot report
leaves the Sandbox sealing for the operator.

## Lifecycle operations

[HarnessHost](../harness_host/README.md) owns Session creation, commands and live status. See the
[Runtime Session protocol](../env/runtime/docs/session_protocol.md#commands-and-effects) for
admission, effects and same-ID retry.
[Executor](../env/runtime/execution/SPEC.md) owns native process and file operations.

During `Quiesce`, Runtime waits for running Session setup and Sandbox bootstrap to finish.
Final admission closes before the journal seals described above.

## Authentication and incarnation binding

Cluster and k3s Environments proxies dial the Runtime's private TLS listener using mTLS and the
`spiffe://agentplane/runtime-proxy` client identity. A bearer cannot replace a certificate. Proxy
callers require exact per-method service SVID grants; public Session callers use Dispatcher.

Standalone behavior tests can explicitly select insecure transport at both endpoints. It supplies
plaintext without peer authentication; it never fabricates a certificate identity. Live Sandbox/Pod/VM
ownership and incarnation validation, Sandbox/incarnation/boot-ID fencing and command admission
remain enforced.
Every Sandbox incarnation gets a credential; k3s keeps real cert-manager/trust-manager and
mTLS in every stack. Bad mTLS credentials never trigger an insecure retry.

The proxy's certificate has URI SAN `spiffe://agentplane/runtime-proxy`. Each Environment Runtime
certificate has exactly one URI SAN, its WorkloadCredential's
`spiffe://agentplane/<namespace>/<sandbox-name>/<credential-id>`.
Certificates must chain to the configured trust roots and permit the peer's client/server EKU.
A CA-valid certificate with the wrong URI is rejected.

Before each workload incarnation the Sandbox Controller requests a credential through the
Sandbox's [WorkloadCredential](workload_authentication.md#workloadcredentials); the Policy
Controller mints its ID and cert-manager issues its certificate into a distinct Secret. The workload carries that ID. Proxy authorization checks the current Sandbox UID, current incarnation Pod, owner
chain and credential ID. VM authorization checks the launcher, VMI and Sandbox-owned VM.
An old credential ID never authorizes a replacement workload. Names and addresses
are not identity evidence.

cert-manager owns leaf renewal through the `agentplane-workload` ClusterIssuer, the
[Agentplane workload CA](workload_authentication.md#the-workload-ca), an intermediate signed by
the cluster internal CA. trust-manager distributes the workload CA alone, which every SVID peer
verifies against. A Pod or KubeVirt runner's control listener verifies the proxy's client SVID
against it (`client_ca_file`), so a `cluster-internal-ca` leaf naming
`spiffe://agentplane/runtime-proxy` is refused. A Firecracker launcher serves the workload CA alone
as the guest's Workload API bundle for the `agentplane` trust domain, so the guest's listener refuses
that leaf too; the guest Runtime receives no cluster bundle. Runtime admits only the
Environments proxy SVID for control calls and the narrow Controller SVID for passive observation;
the retired Gateway SVID is refused even when signed by the workload CA. The Environments proxy's `environment_service.runtime_proxy.runner_security` and the
egress sidecar verify server SVIDs against the workload CA too, so the proxy refuses a
`cluster-internal-ca` leaf naming a runner's URI at that runner's address.
Pods use projected credential files; KubeVirt guests use a mounted Secret filesystem. Firecracker
launchers stream the same material through the host-initiated vsock Workload API to the root guest
agent, which serves Runtime through its restricted Unix socket. Runtime retains pinned credential
snapshots shared by its control server and History client until joined shutdown. Both ends verify
the peer with go-spiffe against their current roots and authorize exactly the expected SPIFFE ID
and EKU. Replaced files and streamed updates provide credentials for subsequent TLS handshakes. Every control call
and observation snapshot rechecks the peer leaf's validity period. Established gRPC connections close
when either selected leaf expires, so a persistent channel reconnects with rotated credentials. A
connection keeps the roots it was verified with until it closes. Workload deletion or replacement is
checked on continuing observation.

The certificate authenticates the Sandbox workload, not every process within it. Code able to read
the runner's private key can act as that workload. No runner certificate authenticates as the
Runtime proxy, and certificates do not replace command authorization or journal deduplication.

## Heartbeat and liveness

`WatchState` publishes a snapshot on a fact change and every 10 seconds while idle. The Controller and Environments proxy end their separate observations after 30 seconds without a
snapshot. Proxy observation also ends on transport failure or identity/ownership loss. Healthy observations have no age limit. It supervises a new observation while the
workload remains current. A live observation establishes control connectivity, not harness readiness,
command admission or native execution. Losing it does not establish that admitted work failed.

## Path through the egress proxy

```mermaid
flowchart LR
    SECRET[(runner credential Secret)]
    subgraph POD[Sandbox Pod]
        RUNTIME[Environment Runtime]
        SIDECAR[egress-sidecar]
        WORKLOAD[workload and executor<br/>no Secret mount]
    end
    PROXY[egress proxy forward listener :8888]
    DEST[policy-approved external destination]
    PLATFORM[first-party service TLS listener]
    SECRET -->|same projected files| RUNTIME
    SECRET -->|same projected files| SIDECAR
    SIDECAR -->|TLS ClientAuth SVID<br/>Kubernetes bearer as destination context| PROXY
    RUNTIME -.->|History TLS over CONNECT| PROXY
    PROXY -->|policy and credential substitution| DEST
    PROXY -.->|opaque TLS tunnel| PLATFORM
```

Runner control stays on the Environments proxy's direct mTLS connection; its private key and control calls do
not pass through HTTP interception. Separately, the egress sidecar relays runner HTTP(S) traffic to
the central proxy. With TLS transport and SPIFFE authentication selected, the sidecar presents the
same per-incarnation Runner certificate to the proxy's TLS-enabled forward listener under
the Sandbox's WorkloadCredential URI. The Kubernetes destination retains its projected
bearer, bound to the Sandbox's recorded Pod; first-party services verify the SVID themselves through
opaque tunnels. The Certificate carries server auth for Runtime control and client auth for this hop.

For the standard runner Pod, the controller mounts one projected Secret, the cluster trust bundle
(`ca.crt`) and the workload CA (`workload-ca.crt`) into the Environment Runtime and egress sidecar; workload and executor containers do not mount it.
Custom runtime-only templates remain supported and receive the projection only in Runtime. For KubeVirt, the guest Runtime reads the same
Secret from its existing root-only virtiofs mount and each bundle from its own virtiofs mount, while the launcher relay gets a projection of that
Secret into its own sidecar mount. For Firecracker, the launcher and relay receive the projection;
the guest Runtime receives the streamed Workload API source, and workload programs cannot read
either the host projection or the root-only guest socket.
Kyverno checks the live Sandbox status binding against the VM annotation, current VMI UID and
credential label before injection. Its admission controller has a namespace-scoped Role granting
only `get` on Sandboxes for that lookup; it does not read the Secret. A missing accepted credential
fails mutation, so KubeVirt retries after the binding is recorded instead of admitting a Pod with
a placeholder Secret name. The sidecar verifies the proxy's fixed SPIFFE identity; the proxy
checks the Runner client identity against current Sandbox/incarnation state.

The sidecar's `proxy` contains an address and a tagged `transport`. TLS owns the CA bundle;
its `auth` variant owns the Runner certificate/key pair. The proxy's configured authentication
policy admits only SPIFFE. A TLS failure ends the request, and files reload on rotation.

The Rules RPC shares that policy and serving TLS material on the native gRPC port 8081. The
workload dials its shared local platform gRPC listener on port 3130. The sidecar opens destination TLS through opaque
central CONNECT for Rules, Notification, Dispatcher, Action
and LLM routes; each service verifies the actual peer itself. Runtime dials History with its own
certificate. Kubernetes retains its destination-audience token, bound to the Sandbox's recorded
Pod. Callers without Sandbox certificates require a separate authentication design.

## Environments receiver

The `environment_service.runtime_proxy` setting enables the Runtime proxy and Executor services on
`environments:8082`. Every replica owns its routing cache and
callable Runtime watches independently of leadership, while the Controller's incarnation-binding
facts watch stays leader-only. This receiver presents the dedicated `runtime-proxy` SVID to Runtime;
callers verify the existing Sandbox Controller server SVID and receive only their exact per-method
service grants. It admits no bearer, workload Identity or delegated proxy calls, and requires no
proxy audience. Environments handles diagnostics, inventory, native delivery, lifecycle and Executor.
Dispatcher calls go to `sessions:8080`.
Enable receiver configuration only after its accepting image is pinned and running. See the
[receiver contract](../environments/SPEC.md#optional-runtime-proxy-and-executor-receiver).

## EnvironmentService diagnostics

`GetRuntimeDiagnostics` gives the App service peer a live snapshot with Environment UID,
workload incarnation, boot ID and observation time. Environments uses its private fenced Runtime
transport and checks the live Sandbox incarnation before and after reading. Offline state returns
an unavailable error and never triggers wake. The [Environments contract](../environments/SPEC.md#live-runtime-diagnostics)
defines its authorization and freshness. The App uses this RPC for on-demand Runtime diagnostics.
Session running marks and the archive guard read Dispatcher activity.
