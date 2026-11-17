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

Clipboard session transfers use a UTF-8 JSON object with exactly `provider` and `session` fields.
The session field must be an object, and the whole transfer is bounded by `MAX_SESSION_BYTES`.
The shared codec takes the expected provider identity from its caller; it owns no provider roster
or provider-specific session schema. Providers validate the returned session object themselves.

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

Completed probes identify whether they were scheduled, manual, verification, startup, or another
kind of provider check. Explicit `Probe` RPC requests accept only the scheduled and manual sources;
the other sources describe checks initiated internally by the provider.

Imports return a single typed result directly. Providers retain no durable import ID or result
history. An `outcomeUnknown` result, timeout, interrupted response or process crash must never
trigger automatic resubmission. The owner must obtain a fresh export before importing again.
The dashboard clears submitted candidate bytes before sending and does not persist them or retry
imports after reload. Session snapshots describe current local state; they do not recover the exact
result of a lost request. Each explicit submission is a new operation: identical candidate bytes
can be submitted again and may be consumed again. There is no content deduplication or replay
protection against an owner deliberately resubmitting a stale export.

The gateway runs as one Go process. Its HTTP/JSON handlers are generated from the protobuf
contract and call the management service in-process; this is the owner-facing management API.
The gateway has no owner-facing gRPC listener. HTTP management requires an owner session or
owner bearer and same-origin browser mutations. Gateway-to-provider gRPC clients use verified
TLS and no automatic retries. `GetInfo` and `GetSession` have a 5-second
status deadline. `Probe`, `ImportSession`, `VerifyCandidate`, and provider HTTP proxy requests use a
120-second operation deadline, with caller cancellation taking effect sooner. The control service serializes all accepted operations and local reads per instance; status reads
can time out while an operation is running. Accepted work continues after caller cancellation. A lost candidate verification or probe response may mean the provider changed upstream credentials;
do not automatically retry it.

`platform_sessions.provider.provider_runtime` composes the provider lifecycle. It acquires
exclusive per-instance process ownership before starting the provider owner. On shutdown it stops
admitting operations, drains accepted operations, stops the control listener and provider owner, and only
then releases the instance locks. If the owner cannot be stopped, ownership remains held until the
process exits.

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

Provider session credentials and the operation fence share the import journal's SQLite database
under each instance's `imports/` directory. Before an exchange that may consume credentials, the
provider commits an in-progress fence and removes the stored credentials. Verified replacement
credentials and clearing that fence commit in one transaction. Interrupted or ambiguous exchanges
require fresh sign-in and import; potentially consumed credentials are never replayed. Session
snapshots use a persisted generation timestamp, independent of filesystem modification times.
Existing JSON session files are ignored and require fresh sign-in and import after deployment.
