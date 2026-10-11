# gRPC services

How Agentplane services define, serve, call and secure gRPC APIs: one way per concern. New code
follows it; code that differs converges on it.

## Schema and generated code

- One `agentplane/proto/<name>.proto` per API, `package agentplane.proto.<name>`.
- Messages build with `py_proto_library_with_mypy_stubs`; the service with `py_grpc_service_library`
  (`<name>_pb2_grpc`, typed by its generated `.pyi`).

## Servicers

- A servicer subclasses the generated `<Service>Servicer`. Each RPC is an `@override` method typed
  from the generated messages:

  ```python
  class HistoryService(protocol_pb2_grpc.HistoryServiceServicer):
      @override
      async def ReadSessionEvents(
          self, request: history_pb2.ReadSessionEventsRequest, context: grpc.aio.ServicerContext
      ) -> history_pb2.ReadSessionEventsResponse: ...
  ```

- It registers through the generated `add_<Service>Servicer_to_server`, which builds the method
  table from the proto's descriptor, so the service's full name and each method's (de)serializers
  can't drift from the schema. No
  `grpc.method_handlers_generic_handler`, no hand-written `*_rpc_method_handler` tables, and no
  `type X = Callable[...]` aliases spelling out handler signatures: the generated base class already
  carries them.
- **Test fakes follow the same rule.** A fake subclasses the servicer and overrides only the RPCs its
  test drives; the inherited ones answer `UNIMPLEMENTED`, which is also how a fake plays a peer that
  predates an RPC. A generic handler is right only where no generated code exists for the service,
  such as a proxy test relaying opaque bytes.
- A servicer takes its collaborators as concrete objects (a frozen `Resources` dataclass once there
  are several). Inject the client or component that owns an operation, not a bare `Callable` for it.

## Authentication

- Callers send `authorization: Bearer <token>` metadata, reading the projected token file for every
  call: projected tokens rotate.
- A servicer admits its caller first thing in each RPC through `agentplane/workload_auth/grpc_caller`:
  `authenticate` checks the caller's account against an allowlist, and `resolve_caller` returns the
  whole principal for a door that authorizes on more than the account (the runner ingress binds a
  Session to its writer; runner control resolves the Sandbox Pod). Services never carry their own
  copy of the bearer parsing.

## Errors

- **Server:** one `errors(context)` async context manager per servicer module wraps each RPC body and
  maps domain exceptions to status codes. Messages are fixed strings: no exception text, Kubernetes
  response, SQL or bearer data reaches a client. A server fault the mapping does not know propagates;
  gRPC logs it and answers `UNKNOWN`. `context.abort` outside the mapping is for request validation
  the RPC itself performs.
- **Client:** the client class translates `grpc.aio.AioRpcError` in one place into its own exception
  carrying the status code (`DEADLINE_EXCEEDED` becomes `TimeoutError`). Upstream `details()` is never
  forwarded.

## Deadlines

- Every unary call passes `timeout=` from the client's settings.
- A long-lived stream carries no call deadline: it bounds its handshake and each write with
  `asyncio.timeout` instead. A watch stream the server bounds by a lease ends with an explicit
  reconnect message.
- A servicer bounds its own backend work with `asyncio.timeout` from its settings.

## Clients

- One `<Service>Client` per API, beside the servicer's owner, owns its channel and stub and exposes
  domain methods. Callers never touch the stub.
- The stub is annotated `<Service>AsyncStub` and built as `<Service>Stub(channel)`.
- Channels are `grpc.aio.insecure_channel`: in-cluster traffic is plaintext and identity comes from
  the bearer (or, for Sandbox workloads, from the egress proxy that substitutes it). Options come from
  `grpc_channel_option_kvps`: the service's configured options plus the client's invariants as
  `required`:
  - `grpc.use_local_subchannel_pool: 1` for every client that closes and redials. From the
    process-wide pool a new channel inherits the closed channel's connection.
  - `grpc.enable_retries: 0` for clients whose mutations must be reconciled, never replayed.

## Lifetimes

Everything that holds a connection is closed by a context manager, never by a hand-written
`try`/`finally: await x.close()`:

- A channel used for one call or one stream session is opened with
  `async with grpc.aio.insecure_channel(...) as channel:`.
- A client class is an async context manager that closes its channel:
  `async with HistoryServiceClient(...) as history:`. A process that owns several enters them on one
  `AsyncExitStack`; a test fixture yields from inside the `async with`.
- `grpc.aio.Server` has no context-manager form, so one shared async context manager registers
  nothing itself but owns the rest: bind, `start`, and `stop` with the given grace on exit. Mains
  and test fixtures use it rather than repeating the sequence.
- Stubs hold no resources of their own and need no lifetime management.

## Server lifecycle

- `grpc.aio.server()`, register servicers, then serve through the shared context manager;
  `stop(grace=5)` on shutdown. A second listener exists only where a network fence admits different callers to it (the
  runner-facing port).
- Health is the standard `grpc.health.v1.Health` service on the service's own gRPC port, probed by
  Kubernetes' native `grpc` liveness and readiness probes. No separate HTTP health server.
- Tests serve on `127.0.0.1:0` with no grace, through the same context manager, which yields the
  bound port. A test builds a `grpc.aio.server()`, registers the real servicer or a fake subclass,
  and enters it; per-service serving helpers add only what is specific to that service (its
  `Resources`, a client already pointed at the port), never their own copy of bind, start and stop.
