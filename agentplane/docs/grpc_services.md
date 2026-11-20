# gRPC services

How Agentplane services define, serve, call and secure gRPC APIs: one way per concern. New code
follows it; code that differs converges on it.

## Schema and generated code

- One `<name>.proto` per API, in the directory of the service that serves it
  (`agentplane/history_service/api/history/history_service.proto`). A messages-only proto that two or more
  services import stays in `agentplane/proto/`.
- `package` is the file's directory with dots for slashes (`agentplane/history_service/x.proto` is
  `agentplane.history_service`, `agentplane/proto/x.proto` is `agentplane.proto`). Files in one directory
  share a package, so their top-level names, enum values included, are unique within it.
- Messages build with `py_proto_library_with_mypy_stubs`; the service with `py_grpc_service_library`
  (`<name>_pb2_grpc`, typed by its generated `.pyi`).
- RPC `X` takes `XRequest` and returns `XResponse`, each its own message even when it wraps a single
  field, so either side can grow without a new method. The exception is a stream whose messages are
  frames of a protocol rather than one call's arguments, like `SessionJournalIngress.Push`'s
  `PushFrame`/`Acknowledgement`.

## Servicers

- A servicer subclasses the generated `<Service>Servicer`. Each RPC is an `@override` method typed
  from the generated messages:

  ```python
  class FakeHistoryService(history_service_pb2_grpc.HistoryServiceServicer):
      @override
      async def ReadSessionEvents(
          self, request: history_service_pb2.ReadSessionEventsRequest, context: grpc.aio.ServicerContext
      ) -> history_service_pb2.ReadSessionEventsResponse: ...
  ```

- It registers through the generated `add_<Service>Servicer_to_server`, which builds the method
  table from the proto's descriptor, so the service's full name and each method's (de)serializers
  can't drift from the schema. No
  `grpc.method_handlers_generic_handler`, no hand-written `*_rpc_method_handler` tables, and no
  `type X = Callable[...]` aliases spelling out handler signatures: the generated base class already
  carries them.
- RPC methods carry `@override`. Besides checking the signature against the generated base, it
  exempts the RPC-cased method names from ruff's `N802`; no `noqa` is needed.
- **Test fakes follow the same rule.** A fake subclasses the generated servicer directly and
  overrides only the RPCs its test drives; the inherited ones answer `UNIMPLEMENTED`, which is also
  how a fake plays a peer that predates an RPC. Deviation from stock mypy-protobuf: the stubs are
  generated with `generate_concrete_servicer_stubs` (`devinfra/python/grpc.bzl`), so mypy sees
  the servicer as the concrete class it is at runtime instead of an ABC with every RPC abstract.
- A fake plays exactly one service. A test peer standing in for two services is two servicers over
  shared state, registered on one server.
- A generic handler is right only where no generated code exists for the service, such as a proxy
  relaying opaque bytes (`environments/runtimeproxy/proxy.go`) or its test.
- A servicer takes its collaborators as concrete objects (a frozen `Resources` dataclass once there
  are several). Inject the client or component that owns an operation, not a bare `Callable` for it.

## Authentication

- First-party backend calls present a service SVID over mutual TLS, verifying the receiver's
  workload-CA chain, exact service URI and `serverAuth` usage. Role-specific service identities
  authenticate the component; exact method grants authorize it. App OIDC and external MCP OAuth
  retain their separate edge authority. Receivers reject Kubernetes bearers. Invalid configured TLS fails without
  downgrading.
- Sandbox workload listeners authenticate the Runtime SVID with the shared
  `peertls` server credentials. `identity/auth.SVIDResolver` resolves
  the verified peer through its live `WorkloadCredential`, yielding the bound Identity and
  any accepted Pod provenance. The sidecar or launcher relay holds the certificate and establishes
  destination TLS through the central proxy's opaque CONNECT tunnel; the proxy asserts no caller.
- A Go server admits callers in one interceptor, `auth.Methods.ServerOptions`
  (`agentplane/identity/auth`): `auth.Methods` maps every served method's full name to a
  `Rule` (`Services` for exact service SVIDs, `AnySubject` with an `SVIDResolver` for workload
  Identities, and `Public` only for the health service through `auth.HealthMethods`). A method it does
  not list is refused before its credentials are resolved, so a new RPC is closed until its Rule is
  written. The owning package exports its `Methods`; handlers consume the typed `auth.Admitted`
  result. `AllowServiceCallers` adds exact direct service peers independently of workload admission.
  `Methods.WithServiceDelegation` adds actor grants to exact served methods. Tests iterate the
  generated `ServiceDesc` with `agentplane/testing/grpcmethods`.
- Resource authorization stays in the handler and uses the identity admitted by the method Rule. For
  example, History ingress binds a Session to its first writer. If a handler needs a richer proof,
  its method Rule and typed context accessor must admit that proof; it does not resolve the request's
  credentials a second time.

Calls made on behalf of an Identity follow the approved
[delegated authorization design](delegated_authorization.md): preserve the authenticated component
actor and effective Identity separately, then apply the Identity's ordinary authorization.
`AllowServiceDelegationFrom` grants that mode to exact service actors on a `Rule` installed for
the method; the direct mode remains separately authorized. `Admitted` returns a typed direct or delegated admission.
Service delegation returns `DelegatedServiceIdentityAdmission`: its actor SVID, effective Identity
and typed provenance remain distinct. Handlers apply the Identity's ordinary grants, never the
actor's component grants. The `identity/auth/grpc` client sends the typed envelope over its
configured service mTLS channel, with no Authorization header or bearer fallback. EnvironmentService
serves its own explicitly configured delegation methods, including separately granted
`ResumeEnvironment`; Dispatcher and History have their own method lists.

First-party service-to-service transport authenticates the component by a client service
SVID over mutual TLS and verifies the receiver's service SVID on every hop. It needs a distinct
service principal and exact per-method service allowlist; a service SPIFFE URI is neither an
`auth.Account` nor a workload `IdentityPrincipal` and supplies no ServiceAccount UID or Pod proof.
For delegated calls, preserve the service actor, effective Identity, and typed provenance separately.
Service admission uses `ServicePrincipal` and exact service allowlists, separately from
workload `SVIDResolver` rules. App OIDC and external MCP OAuth remain separate
edge protocols.

## Errors

- **Server:** a service maps each RPC's error to a status code in one place (`failure` in
  `history_service/internal/rpc/service.go`). Messages are fixed strings: no error text, Kubernetes
  response, SQL or bearer data reaches a client. A failure whose fixed message names no cause logs
  the error behind it.
- **Client:** `agentplane.grpc.calls` logs each failed call and translates `grpc.aio.AioRpcError` in one place into a
  `ServiceError` carrying the status code (`DEADLINE_EXCEEDED` becomes `TimeoutError`; the Runner
  Gateway client reads `FAILED_PRECONDITION` as `RefusedError`).

## Deadlines

- Every unary call passes `timeout=` from the client's settings.
- A long-lived stream carries no call deadline: it bounds its handshake and each write with
  `asyncio.timeout` instead. A Go server bounds the time waiting for each stream `Send` to clear
  transport backpressure ([`streaming.SendWithin`](../grpc/streaming/send.go)); this does not
  guarantee when the peer receives a frame. If that wait times out or its lease/drain context ends,
  the handler returns without another send, which releases the outstanding write.
- A watch stream the server bounds by a lease ends with an explicit reconnect message when the lease
  or a drain expires while it is idle. If either ends during an outstanding write, the handler
  terminates that stream without sending a reconnect frame.
- A servicer bounds its own backend work with `asyncio.timeout` from its settings.

## Clients

- Most APIs use one `<Service>Client` beside the servicer's owner to expose domain methods
  over its stub. The browser uses the app's operator-session BFF to call the generated Dispatcher
  HTTP facade. Agentplane backend callers use the generated native gRPC stub directly and set a deadline.
  Backend caller proof is a service client SVID over verified TLS. Mutations are not automatically retried.
  A client that would call two services is two clients.
- **Clients take channels; the composition root opens them.** A client's constructor takes a
  `grpc.aio.Channel`, never a target string, and builds its stub from it. Whoever constructs the
  client (a `main`, a test fixture) opens the channel with `async with` and closes it; the client
  has no lifetime of its own. Two services behind one test server share one channel. Gotcha: a
  `grpc.aio` channel answers only on the event loop that opened it, so a test whose app serves on
  another loop (a sync `TestClient`) opens its channels there.
- The exception is a channel a client dials for a single call, such as `SandboxControlClient`'s calls
  to the gateway replica holding a Sandbox's runner: the client takes that target and opens and
  closes the channel around the call.
- The stub is annotated `<Service>AsyncStub` and built as `<Service>Stub(channel)`.
- Go clients use `agentplane/grpc/bearer.Dial` with `peertls.ClientTLS` where configured; its token file is optional
  when the caller uses an SVID. The App's generated Policy facade uses `facade.NewHandlerWithOptions` with its
  configured TLS and optional caller credential. Other Go backend routes retain their explicit configured
  transport. Runtime connections that carry an SVID use verified TLS. Python callers that use
  `agentplane/grpc/calls.py` can open a local plaintext channel to the
  credential-holding sidecar; service routes from that sidecar use verified TLS through CONNECT. The sidecar's
  explicit telemetry route to Alloy is plaintext. A client that receives Session Events adds
  `LARGE_EVENT_RECEIVE_OPTIONS` (`agentplane/grpc/options.py`).
  The invariants:
  - `grpc.use_local_subchannel_pool: 1` for every channel that is closed and dialed again to the
    same target. From the process-wide pool a new channel inherits the closed channel's connection.
  - `grpc.enable_retries: 0` for clients whose mutations must be reconciled, never replayed.

## Lifetimes

Everything that holds a connection is closed by a context manager, never by a hand-written
`try`/`finally: await x.close()`:

- Python test servers use local plaintext channels opened with `async with grpc.aio.insecure_channel(...) as channel:`.
  `agentplane.grpc.calls.dial` opens an insecure channel when no channel credentials are supplied and a secure channel
  when they are; production callers close either channel at their composition root. Production Go callers close their
  `grpc.ClientConn` with the configured transport credentials.
- A long-lived channel is opened by the composition root, `async with dial(target) as channel:`,
  and handed to the clients over it. A process that owns several enters them on one `async with`
  or `AsyncExitStack`; a test fixture yields from inside it.
- `grpc.aio.Server` has no context-manager form, so one shared async context manager,
  `agentplane.grpc.serving.serving`, registers nothing itself but owns the rest: bind, `start`,
  and `stop` with the given grace on exit. Mains and test fixtures use it rather than repeating the
  sequence.
- Stubs hold no resources of their own and need no lifetime management.

## Server lifecycle

- `grpc.aio.server()`, register servicers, then serve through the shared context manager;
  `stop(grace=5)` on shutdown. A second gRPC listener is used when a route needs its own transport or network
  boundary. Agentplane service application listeners use TLS; a separate health-only listener
  does not admit application methods.
- A Go server normally serves gRPC with `grpc.Server.Serve`. LLM ingress uses `grpc.Server.ServeHTTP`
  on its shared TLS HTTP/2 listener alongside provider-native inference HTTP; both protocols retain
  service-owned authentication. Servers stop with [`graceful.Stop`](../grpc/graceful/stop.go): calls in flight finish until the shutdown deadline. HTTP
  routes beside it (a facade, agent documentation, metrics, callbacks) take a separate HTTP listener and Service port,
  which stops first, since a facade's calls are gRPC calls.
- Health is the standard `grpc.health.v1.Health` service on the service's own gRPC port, which
  `agentplane.grpc.serving.add_health` registers. Plaintext listeners use native Kubernetes gRPC
  probes; TLS listeners use `/healthprobe`, which verifies the server SVID and calls the same Health RPC.
  No separate HTTP health server.
- Tests serve on `127.0.0.1:0` with no grace, through the same context manager, which yields the
  bound port. A test builds a `grpc.aio.server()`, registers the real servicer or a fake subclass,
  and enters it; per-service serving helpers add only what is specific to that service (its
  `Resources`, a client already pointed at the port), never their own copy of bind, start and stop.
- A fake has no `serve` method: the test registers it with the generated `add_*` like any servicer.
  A fake standing in for two services that a client dials at one target gets one module-level
  `add_<fake>(fake, server)` that registers both, in the generated function's shape.

## Questions for Claude

Other explicit plaintext dial paths reviewed for this change:

- `agentplane/sampler/internal/executor/executor.go`: the direct-executor branch is allowed only with
  `direct_executor_insecure_localtest` and a loopback IP target. It is local transport, not a remote service hop.
- `agentplane/net/peertls/security.go`: Runner control supports an explicit `insecure` mode. Staging selects `mtls`;
  a deployment that selected `insecure` across a network boundary would have a real gap.
- `agentplane/env/runtime/internal/coordinator/session_config.go`: Runtime dials its local model-config sidecar route.
  Pods point to loopback; VM guests use their private launcher-gateway address. Both terminate at the local sidecar or
  relay, which verifies the LLM ingress SVID over TLS, so neither is a remote service hop.
- `agentplane/env/runtime/stream/connection.go` through the non-SVID History ingress fallback: this dials the
  configured History target with a bearer over plaintext. It is a real transport gap wherever that fallback is
  selected; staging uses the Runtime SVID tunnel path. This change leaves that separate fallback untouched.
- `agentplane/egress/sidecar/internal/relay/platform_routes.go`: the sidecar's explicitly plaintext telemetry route
  forwards to Alloy over an in-cluster network connection. This is a real plaintext hop for telemetry; it carries no
  bearer and is not an Agentplane backend service RPC.
- `agentplane/cli/internal/invocation/client.go`: an explicitly configured `http://` service endpoint uses plaintext and
  can carry the CLI's configured bearer. This is a user-directed CLI connection, not an internal service-to-service hop.
- `agentplane/env/runtime/execution/companion/control/control.go` uses a protected Unix socket, and
  `agentplane/firecracker/host/guest_management.go` uses vsock. These are local IPC transports, not network service hops.

## HTTP/JSON facades

HTTP/JSON translates at the client boundary, following the
[service boundary policy](service_boundaries.md#http-and-grpc-boundaries). The integration app serves
browser HTTP and calls backend gRPC directly. A service may also expose a generated facade for its
published direct HTTP-client contract; the app does not proxy requests through it. Serving gRPC alone
does not require an HTTP facade.

- **Schema:** the RPC carries a `google.api.http` rule: `GET /v1/<plural>`, `/v1/<plural>/{id}` or a
  `:watch` custom verb for a stream, with the request's fields as path and query parameters
  (`filter.sessionId=...`, a repeated field repeated). The proto's `go_proto_library` adds
  `//devinfra/go:grpc_gateway` to `compilers`, which emits `Register<Service>HandlerFromEndpoint`.
- **Serving:** [`facade.NewHandler`](../grpc/facade/facade.go) registers the generated handlers at the
  HTTP boundary and targets the backend's native gRPC listener. A direct service facade targets its
  own loopback listener; a browser gateway targets the configured backend. The servicer's
  authentication runs on every call: the gateway sends `Authorization` as `authorization` metadata,
  and a gRPC status becomes its HTTP status with a `{"code", "message"}` body. The app supplies the
  request-bound operator or rotating service credential, never browser-supplied upstream authority.
  Each gateway passes an explicit unary timeout; zero delegates bounds to the servicer. A unary
  timeout never limits a server stream, which ends with its request/session lifetime. Gateway clients
  disable retries so mutations cannot be replayed by the transport.
  **App-owned composition:** the integration app may register generated `HandlerServer` handlers
  against its own in-process unary servicer without a local listener. The app HTTP middleware must
  authenticate callers and enforce cookie CSRF before invocation, because gRPC interceptors do not
  run on this path. Request context and cancellation flow into the servicer. grpc-gateway's in-process
  registration does not support streaming; app-owned watches can forward the same typed responses
  through its standard stream encoder. Calls to other services remain native gRPC, and remotely
  callable gRPC service APIs use a native listener.
- **Exposure:** only an RPC with a rule is on HTTP, so the full-duplex
  `SessionJournalIngress.Push` and the private `Runner` service stay native gRPC. Operator and workload
  callers get separate proto services (`OperatorActionService` beside `ActionService`,
  `NotificationsOperator` beside `Notifications`), each authenticating its own audience. HTTP exposure is a separate choice from the service's implementation language.
- **Wire form:** stock gRPC-Gateway. Fields are camelCase, enums their names, unpopulated fields
  present. A server stream is newline-delimited JSON, one `{"result": <message>}` per line.
- **Browser:** the app serves `/upstream/<service>/v1/` through generated gateway handlers or typed
  adapters over generated stubs, calling backend gRPC directly. Go HTTP boundaries use generated gateway handlers. Neither
  makes an HTTP call to a backend RPC facade.
  The browser decodes ProtoJSON into protobuf-es messages from `ts_proto_library`, which for an
  annotated proto needs the root [`//google/api`](../../google/api/BUILD.bazel) package, and follows a
  stream with [`followWatch`](../app/frontend/transport/watch_stream.ts).
- **Not RPCs, so hand-written HTTP:** OIDC login, callback and logout; `GET /sw.js`; the Electric shape
  proxy; the Sandbox console's WebSocket bridge; the MCP endpoint, OAuth metadata and MCP linkage
  callback; the signed GitHub webhook; LLM ingress; Prometheus metrics.
- **Reference:** where a direct service HTTP API exists, its OpenAPI document can be generated by
  `proto_openapi` ([`//devinfra/go:proto_openapi.bzl`](../../devinfra/go/proto_openapi.bzl)) from the proto.
  The Notification Service keeps its HTTP annotations for the App browser gateway but has no direct
  HTTP workload API, service-local OpenAPI document or `/docs/agents/` routes. Its CLI embeds the agent guides.

Both direct (`grpc/bearer.Dial`) and delegated (`identity/auth/grpc.DialDelegated`) clients
select SVID mode by configuring `ClientTLS.cert_file`, `key_file` and `client_uri` together,
with `ca_file` and the exact `server_uri`. Binary settings validate `client_uri` against the
caller's role. In this mode the token path must be empty. Partial certificate settings, mixed
bearer/SVID settings, a different local certificate identity, or a TLS failure are errors;
none selects a bearer fallback. Certificate and trust files reload on replacement, and gRPC
connections close at either certificate's expiry. Omitting all three client fields retains
server-only TLS with explicit token authentication during the staged migration.
