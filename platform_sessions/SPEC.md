# Platform sessions gateway

The gateway connects configured provider instances to their remote control and data planes. A
provider owns its credentials, persistence, background probes and upstream HTTP behavior. The
gateway authenticates owners, applies route grants, and does not instantiate provider integrations.

The owner management contract is `platform_sessions.management.SessionManagement`. Its HTTP
routes under `/api/instances` are generated from that protobuf contract and use protobuf JSON,
including base64 encoding for candidate bytes. Session snapshots and operation results use the
provider control messages directly. A listed instance without `info` has an unavailable control
service; it is still a configured instance. A failed status RPC is not evidence that no session
exists. A snapshot distinguishes a known session state, which includes its transition time, from
a storage error, whose transition time may be unavailable.

Completed import, verification and probe results use a protobuf oneof named `outcome`, so each
result has exactly one variant and its associated payload travels with that variant. In protobuf
JSON the selected variant is a lower-camel-case member whose value is an object; payload-free
variants use `{}`. For example, probe outcomes are `{"verified":{}}`,
`{"reauthenticationRequired":{}}`, `{"challengeRequired":{}}`,
`{"upstreamUnavailable":{"retryAfter":"3s"}}`,
`{"outcomeUnknown":{"credentialRotationMayHaveOccurred":true}}`,
`{"noSession":{}}`, or `{"failure":{"reason":"PROBE_FAILURE_REASON_STORAGE_ERROR"}}`.
Import outcomes are `{"installed":{}}`, `{"rejected":{"reason":"CANDIDATE_REJECTION_REASON_MALFORMED"}}`,
`{"reauthenticationRequired":{}}`, `{"challengeRequired":{}}`,
`{"upstreamUnavailable":{}}`, `{"outcomeUnknown":{"credentialRotationMayHaveOccurred":true}}`,
or `{"failure":{"reason":"IMPORT_FAILURE_REASON_STORAGE_ERROR"}}`. Candidate verification
uses the same variants except `installed` and `failure` are absent, and `verified` is present.
Common fields such as `instance`, `completedAt` and `source` may appear beside the selected
variant.

`ImportOperation` uses a oneof named `state` with `pending`, `completed` and `unknown` variants.
The first and last are empty objects. `completed` contains the full import response, for example:
`{"instance":{"providerId":"example","instanceId":"primary"},"operationId":"<uuid>","completed":{"instance":{"providerId":"example","instanceId":"primary"},"installed":{}}}`.
No separate state enum or result field is used. A response with no recognized oneof variant is
incomplete or from a newer protocol; callers must handle it conservatively and never infer
success. An explicitly `outcomeUnknown` result includes rotation diagnostics; neither an unknown
result nor a lost response permits automatic retry.

The gateway runs as one Go process. Its HTTP/JSON handlers are generated from the protobuf
contract and call the management service in-process. An optional TLS gRPC listener exposes the
same service using a dedicated owner bearer. HTTP management requires an owner session or owner
bearer and same-origin browser mutations. The provider gRPC clients use verified TLS and no
automatic retries. `GetInfo`, `GetSession`, and `GetImportOperation` have a 5-second status
deadline. `Probe`, `ImportSession`, `VerifyCandidate`, and provider HTTP proxy requests use a
120-second operation deadline, with caller cancellation taking effect sooner. Quest serializes
status reads and operations through one owner lock and browser queue, so status reads can time out
while an operation is running. Each import supplies a client-generated UUIDv4 operation ID. The
provider durably reserves it before dispatch, and reusing the ID never dispatches another import.
The owner reads the operation at `/api/instances/<id>/imports/<operation_id>`; a pending or
unknown result is not success, and a missing record does not prove dispatch was impossible. A
lost candidate verification or probe response may mean the provider changed upstream credentials;
do not automatically retry it.

Provider processes hold exclusive import journal ownership for the entire lifetime of the
provider owner. On shutdown they stop admitting imports, drain accepted imports, stop the control
listener and provider owner, and only then release the journal locks. If the owner cannot be
stopped, ownership must remain held until the process exits.

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
