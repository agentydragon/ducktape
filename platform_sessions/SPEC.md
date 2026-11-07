# Platform sessions gateway

The gateway connects configured provider instances to their remote control and data planes. A
provider owns its credentials, persistence, background probes and upstream HTTP behavior. The
gateway authenticates owners, applies route grants, and does not instantiate provider integrations.

The owner management contract is `platform_sessions.management.v1.SessionManagement`. Its HTTP
routes under `/api/v1/instances` are generated from that protobuf contract and use protobuf JSON,
including base64 encoding for candidate bytes. Session snapshots and operation results use the
provider control messages directly. A listed instance without `info` has an unavailable control
service; it is still a configured instance. A failed status RPC is not evidence that no session
exists.

The gateway checks the owner session or owner bearer for every management HTTP request and requires
a same-origin request for browser mutations. The generated facade and the management gRPC server
listen only on loopback. The facade forwards a separate process-local bearer, checked for each gRPC
call; it must not be exposed to callers. The facade disables gRPC retries and the gateway does not
retry HTTP requests. A failed or lost import, candidate verification or probe response may mean the
provider changed upstream credentials; check session state before attempting another operation.

The provider data plane remains HTTP and is governed by the gateway's route grants. Its endpoints
and provider-specific OpenAPI documents are independent of this management contract. The existing
in-process service retains its legacy owner UI until provider migration is complete.
