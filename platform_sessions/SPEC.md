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

Completed import, verification and probe results carry an outcome enum. Unknown future values
must be handled conservatively. An explicitly unknown outcome includes rotation diagnostics;
neither an unrecognized outcome nor a lost response permits automatic retry.

The gateway runs as one Go process. Its HTTP/JSON handlers are generated from the protobuf
contract and call the management service in-process. An optional TLS gRPC listener exposes the
same service using a dedicated owner bearer. HTTP management requires an owner session or owner
bearer and same-origin browser mutations. The provider gRPC clients use verified TLS, bounded
calls and no automatic retries. A failed or lost import, candidate verification or probe response
may mean the provider changed upstream credentials; check session state before attempting another
operation.

The provider data plane remains HTTP and is governed by the gateway's route grants. Providers
enforce their own upstream header policies and normalize redirects to origin-relative paths under
their upstream base; the gateway prefixes those paths with the instance proxy route. A provider may
advertise an OpenAPI document path in
its control information; the gateway fetches the document from that provider over authenticated
TLS and exposes it with the provider's proxy URL. Provider documents remain in provider images.
The shared gateway image is built and published by Ducktape CI. New source checkouts no longer
include the Python in-process service assembly or its owner-login wiring. This source removal does
not replace an already-built image; a running legacy monolith remains active until deployment
explicitly moves to provider processes and the Go gateway.
