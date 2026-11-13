# Delegated authorization

Status: **approved contract. Shared delegation middleware and the Identity effective-caller path are
implemented across the Runner Gateway, History, LLM ingress, Action Service, and MCP edge consumers.
The central sampler authenticates as its own ServiceAccount and delegates Session effects as the
Identity recorded with that Session. Retained ServiceAccount-attributed Action records are historical
only and do not receive effective-caller delegation. Direct component ServiceAccounts remain valid
service actors through the existing TokenReview path.**
Use this contract when an Agentplane service invokes another service on behalf of an Identity. Direct
callers continue using the [workload authentication](workload_authentication.md) contract.

## Identity and authority

Keep these facts separate:

| Fact             | Source                                                                                                                 | Use                                                                                           |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Actor            | Destination-audience token verified by the receiving backend                                                           | Authenticate the service making this RPC and check its permission to delegate on this method. |
| Effective caller | Trusted delegation envelope: the exact Identity name and UID                                                           | Apply the backend's ordinary caller authorization.                                            |
| Provenance       | The actor's durable record the call serves: an Action request and execution, a Session, or an MCP Connection and grant | Correlate admissions and effects across services.                                             |

A delegated call uses the effective caller's permissions. Membership of the actor in a privileged
backend allowlist must not select a privileged path for that call. Parentage, creator labels,
environment handles and provenance identifiers confer no authority.

The receiving backend explicitly trusts the actor to assert a live Identity on designated methods.
This is a real trust grant: a trusted actor can assert any live Identity in the receiver's policy
namespace on those methods. Configure it separately from ordinary caller admission and privileged
backend access; neither implies permission to delegate. Legacy `caller_namespaces` settings remain
accepted but are inert during rollout. An actor's ordinary ServiceAccount grant never becomes an
Identity grant without the explicit `identity_callers` setting.

For a direct call, actor and caller are the authenticated subject. Agentplane service actors continue
using their own ServiceAccount token for direct component calls. An explicitly privileged broker
operation retains its separately reviewed authority contract; the delegation envelope has no
privilege switch. Action approval authorizes execution of an Action under its contract. It does
not enlarge the effective caller's permissions on a delegated backend RPC.

## Wire contract

Keep domain RPC request/response schemas unchanged. A delegated call carries:

```text
authorization: Agentplane-Delegated <actor's destination-audience SA token>
agentplane-delegation-bin: <serialized Delegation protobuf>
```

**Deviation:** `Agentplane-Delegated` is an Agentplane authorization scheme. It uses the existing
TokenReview mechanism for the token, but explicitly selects delegated authorization. The envelope
uses standard [gRPC binary metadata](https://grpc.io/docs/guides/metadata/); the gRPC library handles
its transport encoding. An HTTP receiver (LLM ingress) takes the same two headers, the envelope in
standard base64 as gRPC itself transports `-bin` metadata, and admits the request through the same
middleware (`Rule.AdmitRequest`).

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
UID remains unknown and cannot be delegated. The original workload token need not survive until
execution; the service uses its own rotating token for the destination and retains the verified
caller Identity instead. No caller-token minting or new signing service is needed for this path.

### Authentication modes and rollout

| Authorization                                  | Delegation metadata                                | Result                                                           |
| ---------------------------------------------- | -------------------------------------------------- | ---------------------------------------------------------------- |
| `Bearer <token>`                               | Absent                                             | Existing direct authentication and explicit service authority.   |
| `Agentplane-Delegated <token>`                 | Exactly one valid envelope                         | Delegated authentication, only on a method admitting that actor. |
| `Bearer <token>`                               | Present                                            | Reject; do not ignore the claimed caller.                        |
| Delegated scheme                               | Missing, repeated, malformed or oversized envelope | Reject before the handler runs.                                  |
| Unknown scheme or repeated authorization value | Any                                                | Reject before the handler runs.                                  |

Sending ordinary `Bearer <Actions-token>` with optional caller metadata is unsafe during a roll:
an older backend could ignore the metadata and use Actions' privileged authority. The distinct
scheme prevents that downgrade. Today's [bearer parser](../identity/auth/auth/auth.go) rejects
it, so an old replica refuses the call before effects. A delegated client never retries using
ordinary `Bearer` authentication. Backends can gain support before clients start using it;
mixed versions may refuse calls but must not elevate them.

Additive protobuf fields must not change authorization meaning when ignored. `identity` remains field
4; the retired ServiceAccount caller tag 1 is reserved. An older receiver that does not recognize
field 4 sees no caller and refuses the Identity envelope before effects. A new receiver rejects raw
legacy tag-1-only bytes because the required Identity is absent. A future incompatible authority
change needs a discriminator that older receivers reject; silently ignoring a new permission
restriction is invalid.

## Receiving a call

Shared middleware in `identity/auth/auth`, composed with the existing per-method rules:

1. Parse one authentication mode and its metadata with bounded input sizes.
2. Verify the actor's token for the receiving service's audience and admitted namespaces. The
   initial Actions delegator must prove its actual Pod-bound workload identity.
3. Check the actor's explicit permission to delegate on this method.
4. Decode and validate the envelope and the actor's Identity grant. A Connection provenance profile
   is additionally method-scoped and requires an Identity effective caller. Resolve the Identity in
   the receiver's policy namespace and require its recorded UID to match; a deleted or replaced
   Identity is a different caller. A method whose direct mode admits no Identity caller refuses a
   delegated one.
5. Admit a typed direct or delegated context. Preserve the actor's proven Pod identity separately
   from the effective Identity; never manufacture Pod proof for the original caller.
6. Run the method's normal resource/policy checks for the effective Identity. For example,
   `SubmitInput` checks it against the Identity incarnation recorded with the Session, while
   `EnvironmentService` compares it with the Sandbox's current exact Identity binding.

The handler consumes the admitted context, not raw identity headers, tool arguments or labels.
No handler-specific copy of delegation parsing or privileged fallback is allowed. A delegated
delegated Identity cannot satisfy a method that requires the caller itself to prove a Pod binding.

Malformed credentials/context yield `UNAUTHENTICATED`; an untrusted delegator, disallowed scope,
UID mismatch or denied resource permission yields `PERMISSION_DENIED`. Failure to verify required
identity/policy state yields `UNAVAILABLE`, subject to the ordinary deadline/cancellation mapping.
Use fixed public messages and retain detailed causes in server diagnostics, without credentials.

Use the shared identity/policy freshness rules, including stream rechecks where required. The
existing positive TokenReview cache can delay observed revocation by up to its configured TTL
(30 seconds by default); this protocol does not promise instantaneous revocation. Reconnects and
new authorization decisions reapply the corresponding checks; provenance never freezes access.

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
that admission. Keep the Session's logical execution principal separate from both its creator and
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
Session whose Sandbox named no Identity has none to delegate, and the sampler then calls as itself
exactly as before Identities existed ([sampler contract](../central_sampler/SPEC.md#identity-and-admission)).
Receivers grant the sampler per method and by scope like any actor: History on `Push`
(`ingress.sampler_identity_callers`) and LLM ingress on inference
(`central_sampler_identity_callers`), both over the Identities of their policy namespace. Keep
the identity and authorization rules above. Do not add speculative generic grants, a
delegation-chain service or arbitrary provenance maps ahead of a consumer.

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
- Delegated EnvironmentService exposes only its exact list/get/watch methods and returns the same target
  facts as direct access for the same Identity UID; foreign targets remain hidden as `NOT_FOUND`.
- Forged callers from ordinary accounts, disallowed methods/namespaces, UID replacement,
  malformed/duplicate metadata and direct/delegated mode confusion are refused before effects.
- Missing required identity/policy state fails closed; watch rechecks and reconnects preserve the
  owning service's revocation behavior.
- An old receiver rejects the new scheme; the client never downgrades to privileged authentication.
- Delayed Action execution preserves the original UID; lost replies retain actor/caller evidence
  and use existing operation reconciliation without blind replay.
- The path runs with the integration app unavailable and the receiver makes no Actions lookup.
  Tests and diagnostics retain correlation identifiers without leaking credentials.

Implementation and rollout work remains tracked in the task DAG; this design document records the
reusable contract, not a claim that a delegated call is currently available.
