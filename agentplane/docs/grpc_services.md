# gRPC services

How Agentplane services define, serve, call and secure gRPC APIs: one way per concern. New code
follows it; code that differs converges on it.

## Schema and generated code

- One `<name>.proto` per API, in the directory of the service that serves it
  (`agentplane/history_service/history_service.proto`). A messages-only proto that two or more
  services import stays in `agentplane/proto/`.
- `package` is the file's directory with dots for slashes (`agentplane/history_service/x.proto` is
  `agentplane.history_service`, `agentplane/proto/x.proto` is `agentplane.proto`). Files in one directory
  share a package, so their top-level names, enum values included, are unique within it.
- Messages build with `py_proto_library_with_mypy_stubs`; the service with `py_grpc_service_library`
  (`<name>_pb2_grpc`, typed by its generated `.pyi`).
- RPC `X` takes `XRequest` and returns `XResponse`, each its own message even when it wraps a single
  field, so either side can grow without a new method. The exception is a stream whose messages are
  frames of a protocol rather than one call's arguments, like `RunnerControl.Connect`'s
  `RunnerFrame`/`ServiceFrame`.

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
  relaying opaque bytes (`runner_gateway/gateway/proxy.go`) or its test.
- A servicer takes its collaborators as concrete objects (a frozen `Resources` dataclass once there
  are several). Inject the client or component that owns an operation, not a bare `Callable` for it.

## Authentication

- Callers send `authorization: Bearer <token>` metadata, reading the projected token file for every
  call: projected tokens rotate.
- A servicer admits its caller first thing in each RPC through `agentplane/workload_auth/go/auth`:
  `Resolver.RequireAccount` checks the caller's account against an allowlist, and `Resolver.Resolve`
  returns the whole principal for a door that authorizes on more than the account (the runner ingress
  binds a Session to its writer; runner control resolves the Sandbox Pod). Services never carry their
  own copy of the bearer parsing.

## Errors

- **Server:** a service maps each RPC's error to a status code in one place (`failure` in
  `history_service/api/service.go`). Messages are fixed strings: no error text, Kubernetes
  response, SQL or bearer data reaches a client. A failure whose fixed message names no cause logs
  the error behind it.
- **Client:** `agentplane.grpc.calls` logs each failed call and translates `grpc.aio.AioRpcError` in one place into a
  `ServiceError` carrying the status code (`DEADLINE_EXCEEDED` becomes `TimeoutError`; the Runner
  Gateway client reads `FAILED_PRECONDITION` as `RefusedError`).

## Deadlines

- Every unary call passes `timeout=` from the client's settings.
- A long-lived stream carries no call deadline: it bounds its handshake and each write with
  `asyncio.timeout` instead. A Go server bounds the time waiting for each stream `Send` to clear
  transport backpressure; this does not guarantee when the peer receives a frame, because an HTTP
  facade may queue the write before flushing it. If that wait times out or its lease/drain context
  ends, the handler returns without another send. Facaded writes attach an abort callback to the
  individual HTTP/2 stream so an outstanding response flush ends too.
- A watch stream the server bounds by a lease ends with an explicit reconnect message when the lease
  or a drain expires while it is idle. If either ends during an outstanding write, the handler
  terminates that stream without sending a reconnect frame.
- A servicer bounds its own backend work with `asyncio.timeout` from its settings.

## Clients

- Most APIs use one `<Service>Client` beside the servicer's owner to expose domain methods
  over its stub. The app's typed HTTP command routes call the matching generated Session
  Dispatcher RPC directly; a small transport object holds the generated stub and uses the shared
  unary helper for rotating bearer, deadline, and status translation. Mutations are not
  automatically retried. A client that would call two services is two clients.
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
- Channels are `grpc.aio.insecure_channel`: in-cluster traffic is plaintext and identity comes from
  the bearer (or, for Sandbox workloads, from the egress proxy that substitutes it). The client's
  package says which options its channels need, through a `dial(target)` the composition root
  calls (`agentplane/grpc/calls.py` for the Runner Gateway and History Service clients); a client
  that receives Session Events adds `LARGE_EVENT_RECEIVE_OPTIONS` (`agentplane/grpc/options.py`).
  The invariants:
  - `grpc.use_local_subchannel_pool: 1` for every channel that is closed and dialed again to the
    same target. From the process-wide pool a new channel inherits the closed channel's connection.
  - `grpc.enable_retries: 0` for clients whose mutations must be reconciled, never replayed.

## Lifetimes

Everything that holds a connection is closed by a context manager, never by a hand-written
`try`/`finally: await x.close()`:

- A channel used for one call or one stream session is opened with
  `async with grpc.aio.insecure_channel(...) as channel:`.
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
  `stop(grace=5)` on shutdown. A second listener exists only where a network fence admits different callers to it (the
  runner-facing port).
- Health is the standard `grpc.health.v1.Health` service on the service's own gRPC port, which
  `agentplane.grpc.serving.add_health` registers, probed by Kubernetes' native `grpc` liveness and
  readiness probes. No separate HTTP health server.
- Tests serve on `127.0.0.1:0` with no grace, through the same context manager, which yields the
  bound port. A test builds a `grpc.aio.server()`, registers the real servicer or a fake subclass,
  and enters it; per-service serving helpers add only what is specific to that service (its
  `Resources`, a client already pointed at the port), never their own copy of bind, start and stop.
- A fake has no `serve` method: the test registers it with the generated `add_*` like any servicer.
  A fake standing in for two services that a client dials at one target gets one module-level
  `add_<fake>(fake, server)` that registers both, in the generated function's shape.

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
- **Exposure:** only an RPC with a rule is on HTTP, so the full-duplex `RunnerControl.Connect` and
  `RunnerIngress.Push` and the tunneled `Runner` service stay native gRPC. Operator and workload
  callers get separate proto services (`OperatorActionService` beside `ActionService`,
  `NotificationsOperator` beside `Notifications`), each authenticating its own audience. HTTP exposure is a separate choice from the service's implementation language.
- **Wire form:** stock gRPC-Gateway. Fields are camelCase, enums their names, unpopulated fields
  present. A server stream is newline-delimited JSON, one `{"result": <message>}` per line.
- **Browser:** the app serves `/upstream/<service>/v1/` through generated gateway handlers or typed
  adapters over generated stubs, calling backend gRPC directly. Go HTTP boundaries use generated gateway handlers; the Python app uses typed adapters. Neither
  makes an HTTP call to a backend RPC facade.
  The browser decodes ProtoJSON into protobuf-es messages from `ts_proto_library`, which for an
  annotated proto needs the root [`//google/api`](../../google/api/BUILD.bazel) package, and follows a
  stream with [`followWatch`](../app/frontend/watch_stream.ts).
- **Not RPCs, so hand-written HTTP:** OIDC login, callback and logout; `GET /sw.js`; the Electric shape
  proxy; the Sandbox console's WebSocket bridge; the MCP endpoint, OAuth metadata and MCP linkage
  callback; the signed GitHub webhook; LLM ingress; Prometheus metrics.
- **Reference:** the service's HTTP reference is the OpenAPI document that `proto_openapi`
  ([`//devinfra/go:proto_openapi.bzl`](../../devinfra/go/proto_openapi.bzl)) generates from the proto,
  so a route and a field are described once, in the proto comment beside them. The service embeds
  it (egress: [`egress/docs/BUILD.bazel`](../egress/docs/BUILD.bazel)), links it from its `/llms.txt`, and
  serves both with `/docs/agents/` through [`facade.MountAgentDocs`](../grpc/facade/docs.go).
  It is OpenAPI 2, the version grpc-gateway emits; an agent reads either.
