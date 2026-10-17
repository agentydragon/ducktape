# Scoped ServiceAccount reads of Session history — proposed contract

Status: **proposed policy design**, not an implemented permission or a grant to existing
callers. Tracked as `THREAD_READ_POLICY_DESIGN` in [the task DAG](task_dag.md).
Review the choices below before implementing the policy. The durable raw Session Event
history and its Thread fold live in the [History Service](../history_service/README.md),
which today serves only its configured reader accounts.

SA-level caller attribution and Sandbox-level authorization are agreed. Reads may request an
individual Session, but the permission covers its Sandbox's histories. V1 supports explicit target
collections with separate membership CRs, the agreed [Sandbox reference modes and launch defaults](multiagent_communication.md#target-collections-and-launch-enrollment),
and no individual-Session grants or computed collection selection.
The [tentative implementation direction](multiagent_communication.md#tentative-implementation-direction)
is service-enforced Session-access `PolicySet` rules assigned through existing `PolicyBinding`s,
with shared evaluation code and enforcement by History on its own API. Exact schemas remain open;
the representation may be revisited if implementation does not work well. No broader auth-engine
replacement is a prerequisite. Sending and reading remain independent.

## What is protected

A logical Session has one stable public ID, its retained raw Events, associated native
observations and evidence, and a Thread view/fold plus operator UI metadata. `read`
permits all available representations of that **same** Session history, including
raw/native evidence; it does not grant runner control, access to the underlying
Sandbox workspace, command admission, sending into another Thread, or creating a
Thread. Day one requires only retained raw reads and follows; folded reads, when added,
come from the same History Service.

Retained history must be readable when the original Pod, Sandbox CR and PVC are gone.
History owns the durable Session ID and its recorded Sandbox association. The chosen policy
representation must keep the facts needed for authorized historical reads after live Kubernetes
objects disappear. Follow the [Kubernetes-resource storage preference](multiagent_communication.md#target-collections-and-launch-enrollment),
with alternatives allowed on concrete implementation friction; historical membership and policy
resource lifetime after Sandbox deletion remain explicit design questions.

## Scope, identity and enforcement

- Associate every history with its recorded Sandbox identity, including the UID; all its Sessions
  use that Sandbox's authorization scope. A Session ID is a lookup address, not a permission
  exception. Do not infer a missing historical association from a reused Sandbox name. Histories
  whose association is unknown remain operator-only until explicitly resolved.
- Grant SAs `read` on specific Sandboxes or explicit collections. Presets can supply initial
  enrollment at authorized launch; later membership removal persists independently. Collection
  membership and permission bundles are separate configurable facts, not a compulsory compartment,
  role hierarchy or agent type. Membership writes follow the
  [agreed administration boundary](multiagent_communication.md#target-collections-and-launch-enrollment).
  Grants remain additive and are checked on every list/read/follow.
- Authenticate workload bearers through TokenReview, retaining verified SA/Pod incarnation evidence.
  Define grant carryover on same-name SA replacement in the chosen policy representation; do not
  assume a caller-supplied name proves an incarnation or trust a forwarded principal header.
- Operator authentication may remain separate for v1; app-auth simplification is optional follow-up
  work. Ingestion/projecting service privileges must not become
  a reusable all-history grant for a hosted agent.

History enforces scoped authorization on its own API regardless of policy representation. Do not
broaden its reader allowlist wholesale: a listed reader reads every Session. Keep read authority
separate from message ingress, Session open/control, Sandbox lifecycle and grant administration.
A read grant does not imply send or create authority.

## Sandbox trust boundary

Sessions sharing a Sandbox share its filesystem, credentials and SA authority. The selected policy
uses one Sandbox scope for all of them; compartment labels do not establish confidentiality inside
it. Work needing incompatible trust or credentials requires separate Sandboxes. Each history path
must resolve the recorded Sandbox association, including Open/adoption and retained historical
reads, so a caller cannot relabel a Session into a differently authorized target. Deleting the
Sandbox never makes its old histories belong to a newly created same-name Sandbox.

## Read and revocation behavior

Session listing is a distinct, weaker read-type permission, independently grantable from sending;
it exposes bounded metadata without transcript content or evidence. Its
[fields](multiagent_communication.md#sandbox-session-discovery-and-provisioning) are selected.
Enumeration and transcript access use the **same Sandbox scope**, with their respective permissions.
Direct Event/history fetch, evidence/frame lookup, retained follow and (if later implemented) folded
reads require history-read permission. Filter before
pagination and totals; an unauthorized ID responds as not found, without existence,
cursor, native payload, or count leaks. Record scope and grant changes in their authoritative owner with audit evidence. Broadening
access to retained history requires an authorized policy or membership edit, not an incidental
preset update.

Use the agreed [policy propagation contract](multiagent_communication.md#policy-propagation-and-freshness):
requests and reconnects evaluate the replica's current watched policy view. Changes propagate
asynchronously, so replicas can briefly disagree. Refuse protected reads until initial sync and
when the required view exceeds its configured freshness limit. Active streams must recheck
authorization and freshness throughout their lifetime, stopping further Event emission when the
replica observes loss of the required grant or its policy view becomes too stale. Authorizing only
at stream-open is insufficient; no global replica-acknowledgement barrier is required for v1.
A revoked reader cannot undo bytes already delivered; the audit must record assignments, widening
and revocation. History lag may be visible to an authorized reader but must not make a fold claim
to cover Events beyond its cursor.

## Implementation readiness and acceptance

Before exposing scoped reads, finalize the tentative rule/membership schemas, concrete administrative RBAC,
freshness settings, historical membership behavior and replacement semantics. Enforce the
reviewed policy and immutable Sandbox association on every history path. Test two Sessions in one authorized
Sandbox, an unrelated denied Sandbox, explicit collection enrollment/removal and an ungranted SA
that discovers nothing. Cover deleted Sandboxes, same-name replacement, unknown associations,
raw/native paths, filtering before pagination, initial sync, stale watches, replicas observing
changes at different times and revocation during a follow. Include
a listing-only SA that can enumerate the granted Sandbox's Sessions but cannot read their histories
or evidence, follow Events or send without the respective additional grants.

Sandbox-wide scope removes the proposed per-Session compartment distinction. No separate
compartment-placement implementation is required for this read policy; association correctness and
the existing Sandbox isolation boundary remain acceptance requirements.
