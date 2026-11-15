# Delegated authorization

Status: **approved and implemented contract. First-party service actors prove their role with a
service SVID over mTLS. The typed envelope names the effective Identity and provenance; no
Kubernetes bearer authenticates a service actor.** The central sampler delegates Session effects
as the Identity recorded with that Session. Retained ServiceAccount-attributed Action records are
historical only and do not receive effective-caller delegation.
Use this contract when an Agentplane service invokes another service on behalf of an Identity. Direct
callers continue using the [workload authentication](workload_authentication.md) contract.

## Identity and authority

Keep these facts separate:

| Fact             | Source                                                                                                                 | Use                                                                                           |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Actor            | Verified client service SVID admitted by the receiving backend                                                         | Authenticate the service making this RPC and check its permission to delegate on this method. |
| Effective caller | Trusted delegation envelope: the exact Identity name and UID                                                           | Apply the backend's ordinary caller authorization.                                            |
| Provenance       | The actor's durable record the call serves: an Action request and execution, a Session, or an MCP Connection and grant | Correlate admissions and effects across services.                                             |

A delegated call uses the effective caller's permissions. Membership of the actor in a privileged
backend allowlist must not select a privileged path for that call. Parentage, creator labels,
environment handles and provenance identifiers confer no authority.

The receiving backend explicitly trusts the actor to assert a live Identity on designated methods.
This is a real trust grant: a trusted actor can assert any live Identity in the receiver's policy
namespace on those methods. Configure it separately from ordinary caller admission and privileged
backend access; neither implies permission to delegate. Legacy `caller_namespaces` settings never
grant authority; strict service settings reject them. A direct service allowlist never grants
permission to assert an effective Identity; service delegation needs its own explicit method grant.

For a direct call, actor and caller are the authenticated subject. Agentplane services present
their role's service SVID for direct component calls. An explicitly privileged broker
operation retains its separately reviewed authority contract; the delegation envelope has no
privilege switch. Action approval authorizes execution of an Action under its contract. It does
not enlarge the effective caller's permissions on a delegated backend RPC.

## Wire contract

Keep domain RPC request/response schemas unchanged. A delegated call proves the actor with its
service certificate over mutual TLS and carries exactly one envelope, with no Authorization header:

```text
agentplane-delegation-bin: <serialized Delegation protobuf>
```

The receiver verifies the certificate chain, client-auth usage and exact service SPIFFE ID against
its explicit actor grant for the method. A service URI is never resolved to an agent Identity.
The envelope uses standard [gRPC binary metadata](https://grpc.io/docs/guides/metadata/); the gRPC
library handles its transport encoding. An HTTP receiver (LLM ingress) takes the envelope in
standard base64, as gRPC transports `-bin` metadata, and admits the verified TLS peer and envelope
through the same middleware (`Rule.AdmitRequest`). Neither a missing nor a failed certificate
falls back to bearer proof.

The envelope is `Delegation` in [`delegation.proto`](../proto/delegation.proto), a messages-only
schema in package `agentplane.proto` that reuses `Identity` from
[`identity.proto`](../proto/identity.proto). Its effective caller is a `oneof` containing `identity`,
an Identity in the receiver's policy namespace. The retired ServiceAccount caller field is reserved;
legacy bytes cannot be interpreted as an effective Identity. Its provenance is a
second `oneof` of typed profiles: `action`, the Action Service's request and execution records;
`session`, the central Session a sampler acts for; or `connection`, an external MCP Connection and
its existing consented grant.

All fields are required by this profile's validation: exactly one valid Identity name with a
nonempty UID and exactly one provenance profile with valid UUIDs (a Session ID in its canonical
spelling). Neither the actor
nor its credentials appear in this envelope. The Action execution ID is the existing execution
record's ID, never its lease token. A Connection and grant ID identify existing authority for audit
attribution, but confer no authority by themselves; only an explicitly opted-in receiving method
may admit Connection provenance, and it requires an Identity effective caller. A receiver consumes
the profile its method serves: the
Dispatcher records an Action's correlation, History and LLM ingress require the Session a sampler
names to be the one the call is about.

Actions constructs the envelope from its authenticated, durable Identity request and execution
records. Tool arguments cannot supply or override it. Admission retains the caller Identity UID with
the request and the existing execution UUID with the dispatch payload. A historical ServiceAccount
request remains attributed to its original caller and receipt, but execution fails closed before a
backend call; it is never re-attributed or replayed. A historical Identity request without a stored
UID remains unknown and cannot be delegated. The original workload connection need not survive
until execution; the service presents its own rotating certificate and retains the verified caller
Identity instead. No caller-token minting or new signing service is needed for this path.

### Credential selection

On delegated backend methods, credential selection follows these rules. Operator OIDC and MCP
OAuth edge methods retain their own authority checks.

| Peer proof                                | Delegation metadata                       | Result                                                                 |
| ----------------------------------------- | ----------------------------------------- | ---------------------------------------------------------------------- |
| Verified service SVID                     | Exactly one valid envelope                | Delegation only for an explicitly granted service actor and method.    |
| Verified service SVID                     | Absent                                    | Direct admission only through the method's separate service grant.     |
| Workload SVID                             | Absent                                    | Direct workload Identity admission through the method's workload rule. |
| Missing, invalid or untrusted certificate | Any                                       | Reject before the handler runs.                                        |
| Any SVID plus Authorization metadata      | Any                                       | Reject ambiguous credentials.                                          |
| Verified service SVID                     | Repeated, malformed or oversized envelope | Reject before the handler runs.                                        |
| Workload SVID                             | Present                                   | Reject; a workload certificate is not a service actor.                 |

A client never retries a failed delegated call as a direct privileged call. Legacy
`Agentplane-Delegated` tokens and ordinary Kubernetes `Bearer` tokens grant no receiver admission.
An older receiver that does not support service-certificate delegation must refuse the call;
mixed versions must not elevate it by ignoring the effective caller.

Additive protobuf fields must not change authorization meaning when ignored. `identity` remains field
4; the retired ServiceAccount caller tag 1 is reserved. An older receiver that does not recognize
field 4 sees no caller and refuses the Identity envelope before effects. A new receiver rejects raw
legacy tag-1-only bytes because the required Identity is absent. A future incompatible authority
change needs a discriminator that older receivers reject; silently ignoring a new permission
restriction is invalid.

## Receiving a call

Shared middleware in `identity/auth`, composed with the existing per-method rules:

1. Parse one authentication mode and its metadata with bounded input sizes.
2. Verify the actor's service certificate against the workload trust bundle, client-auth usage
   and exact service SPIFFE ID. The certificate supplies no Identity or Pod proof.
3. Check the actor's explicit permission to delegate on this method.
4. Decode and validate the envelope and the actor's Identity grant. A Connection provenance profile
   is additionally method-scoped and requires an Identity effective caller. Resolve the Identity in
   the receiver's policy namespace and require its recorded UID to match; a deleted or replaced
   Identity is a different caller. The method must explicitly admit service delegation for that
   actor; a direct component grant alone is insufficient.
5. Admit a typed direct or delegated context. Keep the actor's service principal separate from the
   effective Identity; neither supplies Pod proof for the original caller.
6. Run the method's normal resource/policy checks for the effective Identity. For example,
   `SubmitInput` checks it against the Identity incarnation recorded with the Session, while
   `EnvironmentService` compares it with the Sandbox's current exact Identity binding.

The handler consumes the admitted context, not raw identity headers, tool arguments or labels.
No handler-specific copy of delegation parsing or privileged fallback is allowed. A
delegated Identity cannot satisfy a method that requires the caller itself to prove a Pod binding.

Malformed credentials/context yield `UNAUTHENTICATED`; an untrusted delegator, disallowed scope,
UID mismatch or denied resource permission yields `PERMISSION_DENIED`. Failure to verify required
identity/policy state yields `UNAVAILABLE`, subject to the ordinary deadline/cancellation mapping.
Use fixed public messages and retain detailed causes in server diagnostics, without credentials.

Use the shared identity/policy freshness rules, including live effective-Identity UID lookup and
stream rechecks where required. Revalidation rechecks the actor certificate and method grant as
well as the effective Identity. Reconnects and new authorization decisions reapply those checks;
provenance never freezes access. TokenReview caching is not part of this protocol.

## Actions, persistence and recovery

Actions retains the verified caller Identity UID at admission and carries it through Decision,
execution and worker payloads. A caller-delegating adapter uses that durable Identity. A historical
request attributed to a ServiceAccount remains readable as an Action receipt and audit record, but
its execution fails closed before calling a backend. An Action without a recorded Identity UID cannot
silently acquire the UID of whichever Identity now has the same name.
Stored schema changes use new migrations and the normal service startup lifecycle.

The backend does not call Actions to resolve the request or validate approval on every RPC. It
trusts the admitted delegator's assertion and applies its own authorization. Action IDs are audit
correlation, not capabilities or evidence that a human approved anything.

Where a backend durably admits an operation, retain actor, effective caller and provenance with
that admission. Native `CreateSession` applies the effective Identity's current `bind_session`
grant; the new Session executes as the placed Sandbox's exact live Identity. Its creator may recover
only the exact creation receipt after grant removal; creator status grants no Session read, command
or attachment authority. Keep the Session's logical execution principal separate from both its creator and
the peer writing evidence. Do not store bearer tokens or Action lease tokens in these records.

Delegation changes neither idempotency nor operation ownership. Existing Session/command IDs,
argument consistency and authorized lookups govern lost replies. Reuse the operation identity for
reconciliation; matching Action IDs alone do not deduplicate effects. Do not automatically replay a
mutation whose outcome is unknown. Resumed workers reacquire their own credentials and reauthorize
the pinned caller rather than treating an earlier admission as permanent permission.

## Applying the design elsewhere

Services needing caller delegation reuse the authentication mode, actor/caller distinction,
per-method trust checks and shared client/server implementation. No application or Actions lookup
is required by the receiving backend. A caller need not have a Session or an execution environment.

A concrete non-Action consumer adds a typed provenance profile through shared schema evolution;
it must not invent Action IDs or require Actions to be deployed. The central sampler is one: it
authenticates as itself and holds no agent key, and each call's effective caller is the Identity
the Dispatcher recorded the Session as executing as, read from the sampler's own Session record,
with the Session as provenance; neither JavaScript nor tool arguments can name or override it. A
Session's environment attachments do not establish its effective caller. Each consumer applies
its own contract to the retained execution Identity; missing proof never becomes the actor's
Identity or a same-named successor. History's sampler grant is `ingress.sampler_service_actor`;
LLM ingress separately grants `central_sampler_services` and `central_sampler_identity_callers`.
Neither grant is a requirement that all Sessions or retained History records have a Sandbox,
a single environment attachment or an execution Identity. Do not add speculative generic grants,
a delegation-chain service or arbitrary provenance maps ahead of a consumer.

Trust is not transitive. A further hop needs its own explicitly trusted actor and target method.
Construct outgoing context from an admitted identity or trusted durable record; never blindly
forward incoming authorization/delegation metadata. Backend evidence transfer and privileged
maintenance keep their own domain contracts rather than becoming caller delegation implicitly.

Direct CLI/MCP paths can continue to authenticate the caller through their existing credential
mechanisms. They apply the same resource authorization without requiring this envelope. Kubernetes
API impersonation does not itself authenticate callers to Agentplane's gRPC services.

## Implementation acceptance

Before exposing the delegated path, demonstrate:

- Direct and delegated calls by the same effective caller receive the same ordinary access checks;
  an actor's privileged membership cannot widen delegated access.
- Delegated EnvironmentService returns the same inventory facts as direct access for the same Identity
  UID. Explicit resume checks the effective Identity's current `environment` grant, independently of
  the actor. Refused targets the caller cannot read remain hidden as `NOT_FOUND`.
- Forged callers from ordinary accounts, disallowed methods/namespaces, UID replacement,
  malformed/duplicate metadata and direct/delegated mode confusion are refused before effects.
- Missing required identity/policy state fails closed; watch rechecks and reconnects preserve the
  owning service's revocation behavior.
- A receiver without the service-actor grant refuses the call; the client never downgrades to
  direct privileged or bearer authentication.
- Delayed Action execution preserves the original UID; lost replies retain actor/caller evidence
  and use existing operation reconciliation without blind replay.
- The path runs with the integration app unavailable and the receiver makes no Actions lookup.
  Tests and diagnostics retain correlation identifiers without leaking credentials.

The task DAG tracks remaining implementation cleanup and rollout gates. This document records the
shared authorization contract; each destination still owns its method grants and resource checks.
