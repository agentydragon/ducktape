# Platform Sessions

## Goal

For each provider, the owner should do bounded sign-in work that does not grow with the number of
requests made by agents or other consumers. Depending on the provider, sign-in may involve importing
a HAR or Chrome session, or interacting with a hosted browser such as a noVNC session. A sign-in can
involve multiple steps and provider challenges.

Platform Sessions should keep the resulting session alive and renew it as far as the provider permits,
so authorized agents and services can use the provider's internal APIs through its data plane without
owner involvement in routine requests. This is a best-effort goal: some providers may require a new
sign-in or challenge periodically, or whenever a session cannot be maintained.

## Gateway

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

Provider instances may advertise a public sign-in page in `ProviderInstanceInfo.login_url`
(`loginUrl` in protobuf JSON). The owner UI shows a visible sign-in link on each card outside
its export instructions, opening a new tab with `noopener noreferrer`. The URL must be absolute
HTTPS with a hostname and no userinfo, whitespace, control characters, or backslashes. The UI
omits absent, malformed, or unsafe URLs, including authorities that browser parsing would rewrite;
the card and its remaining controls stay available when metadata or its URL is unavailable.
Providers own these destinations; the shared UI has no provider-specific URL roster.

An instance may also advertise `interactiveSignIn` in `GetInfo`. Presence means the provider
serves a graphical sign-in browser at the fixed private WebSocket path
`/internal/interactive-sign-in/{instance_id}`. The gateway derives this path from its configured
instance identifier, authenticates the owner and forwards only to that configured provider over
verified TLS with a provider bearer. The provider independently verifies the bearer on every
WebSocket connection. Neither metadata nor the browser can select another provider endpoint.
When `initial_url` is omitted, the shared runtime does not navigate upstream at startup. It closes
extra tabs currently exposed by Playwright and moves the remaining tab to `about:blank` before
reporting ready. Such providers
navigate in `on_viewer_connect` after authenticated admission and before viewer acceptance. Chromium
may issue a transient request while restoring a profile before Playwright exposes the context, so
providers quiesce visible pages before orderly shutdown as well.
The experimental `launch_mode="cdp"` starts ordinary Chromium without a startup window,
then attaches Playwright over a random loopback-only CDP port. It installs permanent HTTP(S)
and WebSocket fallback guards before creating a visible page. Provider request routes must use
`continue_()` for their explicitly allowed requests; `fallback()` remains denied. Only the
page selected by `run_viewer_sync` can use unscoped HTTP(S) and WebSockets during authenticated
viewer admission. Disconnect revokes that allowance before the provider quiesces the page.
Restoration may continue after startup and produce blocked Chromium error pages. Those tabs
remain denied; `run_viewer_sync` closes other known tabs before allowing its selected page.
This mode requires `service_workers="block"`, refuses profiles containing service-worker
storage before process launch, and blocks new registrations before page scripts run. Chromium
can create worker storage even with registration blocked: a caller using a dedicated managed
profile must explicitly clear that regenerable storage before restart, or use a fresh profile
and sign in again. The shared runtime never deletes profile data. Optional HAR recording
uses the attached context and is finalized during orderly shutdown.
This is a canary experiment, not process-level network isolation. Its guards cover page
HTTP(S) and WebSockets; Chromium background services and other protocols are outside that
boundary. Background networking, extensions and QUIC are disabled, but the absence of all
pre-attachment process egress is not guaranteed. The default Playwright launch path is unchanged.
The public upgrade requires the owner's session cookie and an exact public `Origin`, even though
ordinary management GETs allow a missing Origin. A bearer token alone cannot open the viewer.
The gateway relays binary RFB frames without applying the 120-second HTTP operation deadline;
it closes the stream when either peer disconnects or the owner's session expires. The page CSP
allows WebSocket connections only to the configured public origin.
One owner viewer is admitted per instance at a time. A provider invalidates prior verification
before admitting the viewer and suppresses automatic data-plane reads while the viewer is connected.
Disconnect without a successful Finish leaves the session unverified; an expected disconnect after
a successful Finish preserves the newly verified session. The browser profile survives reconnects in a
private per-instance state directory. `FinishInteractiveSignIn` is an explicit owner operation:
the provider verifies the upstream account using the live browser, persists the resulting session,
then returns a `ProbeResult` with source `PROBE_SOURCE_VERIFY`. The gateway does not retry a lost
finish response; status remains available for the owner to inspect. A finish failure or viewer
disconnect cannot by itself establish that a usable session was installed.
Export command lines remain selectable and have a copy button that writes the exact displayed
command to the browser clipboard. A successful clipboard write briefly confirms the copy; a failed
write displays an error and leaves manual copying available. The button does not copy session data.

Clipboard session transfers use a UTF-8 JSON object with exactly `provider` and `session` fields.
The session field must be an object, and the whole transfer is bounded by `MAX_SESSION_BYTES`.
The shared codec takes the expected provider identity from its caller; it owns no provider roster
or provider-specific session schema. Providers validate the returned session object themselves as
part of import; a candidate is installed only after that validation succeeds. A provider may fence
the previous credentials before upstream validation that can rotate them, so rejection or an
ambiguous result after dispatch does not guarantee that the previous session remains usable.

Completed import and probe results use a protobuf oneof named `outcome`, so each result has exactly
one variant and its associated payload travels with that variant. In protobuf
JSON the selected variant is a lower-camel-case member whose value is an object; payload-free
variants use `{}`. For example, probe outcomes are `{"verified":{}}`,
`{"reauthenticationRequired":{}}`, `{"challengeRequired":{}}`,
`{"upstreamUnavailable":{"retryAfter":"3s"}}`,
`{"outcomeUnknown":{"credentialRotationMayHaveOccurred":true}}`,
`{"noSession":{}}`, or `{"failure":{"reason":"PROBE_FAILURE_REASON_STORAGE_ERROR"}}`.
Import outcomes are `{"installed":{}}`, `{"rejected":{"reason":"CANDIDATE_REJECTION_REASON_MALFORMED"}}`,
`{"reauthenticationRequired":{}}`, `{"challengeRequired":{}}`,
`{"upstreamUnavailable":{}}`, `{"outcomeUnknown":{"credentialRotationMayHaveOccurred":true}}`,
or `{"failure":{"reason":"IMPORT_FAILURE_REASON_STORAGE_ERROR"}}`. There is no candidate-only
response; upstream checks happen as part of import before installation. The import response carries
an instance reference beside the selected outcome.

Completed probes identify whether they were scheduled, manual, verification, startup, or another
kind of provider check. Explicit `Probe` RPC requests accept only the scheduled and manual sources;
the other sources describe checks initiated internally by the provider.

`ImportSession` validates the submitted session against the upstream before installing it; there
is no separate candidate preflight API. Local parsing and request validation happen before dispatch
and preserve the installed session. Once a provider dispatch may consume or rotate upstream
credentials, a failed or ambiguous import can require fresh sign-in and import.

Imports return a single typed result directly. Providers do not write durable import ID or result
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
status deadline. `Probe`, `ImportSession`, `FinishInteractiveSignIn`, and provider HTTP proxy requests use a
120-second operation deadline, with caller cancellation taking effect sooner. The control service
serializes all accepted operations and local reads per instance; status reads can time out while an
operation is running. Accepted work continues after caller cancellation. A lost import or probe
response may mean the provider changed upstream credentials; do not automatically retry it or assume
the previous session remains usable.

`platform_sessions.provider.provider_runtime` composes the provider lifecycle. It acquires
exclusive per-instance process ownership before starting the provider owner. On shutdown it stops
admitting operations, drains accepted operations, stops the control listener and provider owner,
and only then releases the instance locks. If the owner cannot be stopped, ownership remains held until the
process exits.

The provider data plane remains HTTP and is governed by the gateway's route grants. Providers
enforce their own upstream header policies and normalize redirects to origin-relative paths under
their upstream base; the gateway prefixes those paths with the instance proxy route. A provider may
advertise an OpenAPI document path in
its control information; the gateway fetches the document from that provider over authenticated
TLS and exposes it with the provider's proxy URL. Provider documents remain in provider images.
The shared gateway image is built and published by Ducktape CI.

Provider credentials and the durable operation fence are stored in the `sessions` table of
`imports/import-operations.sqlite3` below each instance's state directory. Before an exchange that
may consume credentials, the provider commits `pending = true` and clears the stored credentials.
Verified replacement credentials and clearing the fence commit in one transaction. Interrupted or
ambiguous exchanges require fresh sign-in and import; potentially consumed credentials are never
replayed. Session snapshots use a persisted generation timestamp, independent of filesystem
modification times.
