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

- Implement and register generated server types. Python subclasses `<Service>Servicer`,
  annotates RPC methods with `@override` (also satisfies Ruff `N802`), and registers with
  `add_<Service>Servicer_to_server`. Go embeds `Unimplemented<Service>Server` and uses
  `Register<Service>Server`. Use a generic handler for an opaque pass-through such as the
  [Environments Runtime proxy](../environments/runtimeproxy/proxy.go).
- Inject concrete clients or components. Group several Python collaborators in a frozen
  `Resources` dataclass.
- A test fake implements one generated service and overrides only the exercised RPCs. The
  inherited methods answer `UNIMPLEMENTED`. A peer serving two fakes registers both on one server
  and can share their state. Python uses concrete generated stubs
  ([`generate_concrete_servicer_stubs`](../../devinfra/python/grpc.bzl)).

## Authentication

- Follow [workload authentication](workload_authentication.md) and the
  [service and workload admission rules](../identity/auth/README.md). First-party backend calls
  use verified service mTLS. App OIDC and external MCP OAuth authenticate their own edge callers.
- Each Go service exports its `auth.Methods` policy and installs `Methods.ServerOptions`. Test it
  against the generated `ServiceDesc` with [`grpcmethods`](../testing/grpcmethods/).
  Handlers use `auth.Admitted` for resource checks. When a handler needs additional proof,
  admit it in the method rule and consume it from the typed admission.
- [Delegated authorization](delegated_authorization.md) gives an exact service actor permission
  to present a typed effective Identity and provenance for an explicit method. Handlers apply
  the effective Identity's usual resource grants. Direct service grants are independent.

## Errors

- **Server:** a service maps each RPC's error to a status code in one place (`failure` in
  `history_service/internal/rpc/service.go`). Give callers specific, actionable causes without
  exposing credentials or bearer data. Service APIs also serve workload and external identities;
  do not assume every caller has operator authority. Preserve additional diagnostics for the
  authenticated operator through an appropriately authorized path. An unexpected failure that
  cannot safely carry its cause logs the detail and identifies the failed operation to the caller.
  See [error visibility](principles.md#keep-the-information-make-dropping-it-a-decision).
- **Client:** `agentplane.grpc.calls` logs each failed call and translates `grpc.aio.AioRpcError` in one place into a
  `ServiceError` carrying the status code (`DEADLINE_EXCEEDED` becomes `TimeoutError`; `call_unary`
  reads `FAILED_PRECONDITION` as `RefusedError`).

## Deadlines

- Set unary deadlines from client settings and bound server-side backend work (use
  `asyncio.timeout` in Python).
- Long-lived streams carry no overall deadline. Bound the handshake and each write separately
  with Python's `asyncio.timeout` or a Go context. [`streaming.SendWithin`](../grpc/streaming/send.go)
  bounds a Go server's write under transport backpressure.
  After a write timeout or cancellation, return from the handler to release any outstanding
  send. Leased watches that define a reconnect frame can send it when the lease ends or the
  server drains while idle.

## Clients

- Keep one domain client per service beside the servicer. First-party backend callers use
  generated native stubs. In Python annotate the stub `<Service>AsyncStub`.
- The composition root opens and closes the channel and passes it to the client. Python
  fixtures yield inside an `async with` on the loop that uses the channel, including test apps
  served on another loop. Share a channel for services on the same test server.
  [`SandboxControlClient`](../session_dispatcher/client.py) dials the Environments proxy per RPC.
- [Python `dial`](../grpc/calls.py) sets the large-event receive limit and a local subchannel
  pool. The local pool prevents a fresh per-RPC channel from inheriting the closed channel's
  replica connection. Keep transport retries disabled for calls requiring caller reconciliation.
- For Go service SVIDs use [`peertls.ClientTLS`](../net/peertls/client_tls.go) with the exact role
  URI and server URI. The [direct](../grpc/bearer/bearer.go) and
  [delegated](../identity/auth/grpc/client.go) dialers take an empty token path in this mode.
  Partial or mixed settings fail validation. Certificate and trust files reload on replacement,
  and gRPC connections close at either certificate's expiry.
- Runtime model config uses the local sidecar or VM relay, which verifies the LLM ingress SVID
  through CONNECT. See [model configuration](../llm_ingress/README.md#per-model-client-configuration).
  The separate [telemetry route](system_overview.md#telemetry) sends plaintext OTLP to Alloy.
  [Runtime journal delivery](../history_service/README.md#runner-ingress) uses workload
  mTLS. The [ingress supervisor](../env/runtime/ingress/supervisor.go) retains a legacy plaintext
  bearer branch that the current History listener refuses. Runtime tests use a plaintext peer.
- See [Runtime test transport](runner_control.md#authentication-and-incarnation-binding),
  [Sampler's loopback-only test executor](../sampler/README.md#running) and
  [CLI endpoint configuration](agent_cli.md#configuration-and-credentials) for their explicit
  transport choices. The executor companion uses a [Unix socket](../env/runtime/execution/companion/control/control.go).
  Firecracker management uses [vsock](../firecracker/SPEC.md).

## Server lifecycle

- Go servers use `grpc.Server.Serve` and [`graceful.Stop`](../grpc/graceful/stop.go). LLM ingress
  uses `ServeHTTP` on its shared TLS HTTP/2 listener alongside provider-native inference HTTP,
  with separate application authentication for each protocol. Other HTTP routes use a separate
  listener and Service port that stops before gRPC, so facade calls can finish.
- Python uses [`serving`](../grpc/serving.py) to bind, start, and stop registered servicers.
  Tests bind `127.0.0.1:0` and use zero grace. Use five seconds of grace on service shutdown.
- Use standard gRPC Health. Native Kubernetes probes call plaintext health listeners, and
  [`healthprobe`](../grpc/healthprobe/main.go) verifies the server SVID for TLS listeners.
  Separate health-only listeners expose no application methods. HTTP application listeners own
  their own health routes, such as [LLM ingress's public `/healthz`](../llm_ingress/README.md).

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
  approved request-bound credential or its configured service SVID. Browser credentials authenticate
  to the app.
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
  `SessionJournalIngress.Push` and the private `EnvironmentRuntime` service stay native gRPC. Operator and workload
  callers get separate proto services (`OperatorActionService` beside `ActionService`), each authenticating its own audience. HTTP exposure is a separate choice from the service's implementation language.
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
