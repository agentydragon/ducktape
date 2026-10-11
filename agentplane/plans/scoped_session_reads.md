# Scoped ServiceAccount reads of Session history — proposed contract

Status: **proposed policy design**, not an implemented permission or a grant to existing
callers. Tracked as `THREAD_READ_POLICY_DESIGN` in [the task DAG](task_dag.md)
(as expanded in [PR #9411](https://github.com/agentydragon/ducktape/pull/9411)).
Review the choices below before implementing the policy. The durable raw Session Event
history and its Thread fold live in the [History Service](../history_service/README.md),
which today serves only its configured reader accounts.

## What is protected

A logical Session has one stable public ID, its retained raw Events, associated native
observations and evidence, and a Thread view/fold plus operator UI metadata. `read`
permits all available representations of that **same** Session history, including
raw/native evidence; it does not grant runner control, access to the underlying
Sandbox workspace, command admission, sending into another Thread, or creating a
Thread. Day one requires only retained raw reads and follows; folded reads, when added,
come from the same History Service.

Retained history must be readable when the original Pod, Sandbox CR and PVC are gone.
The history authority, not live Kubernetes inventory, owns the durable Session ID,
its classification and grants. Legacy Sessions retain their current public UUID and
map to their unchanged `(sandbox, runner session_id)` storage locator while it exists.

## Proposed scope and principal

- A Session has **at most one** operator-defined compartment. Compartments are opaque,
  independent names: no hierarchy, prefix inheritance, implicit ownership or
  persistent Agent type. An unclassified Session has no ServiceAccount-readable
  history. Existing histories are unclassified until an authorized operator assigns
  them; no grant is inferred from a former launch preset or Sandbox name.
- An operator grants `read` on explicitly named compartments to a Kubernetes
  ServiceAccount; presets may suggest a new Session's initial classification but are
  not grant authorities. A Thread and its logical Session share classification.
  The grant is evaluated on **every** list/read/follow, not stamped into a token or
  inherited from the caller's own Thread. Exact-Session exceptions can be added
  later if a concrete need appears; avoid multiple labels and OR semantics initially.
- Continue to authenticate Pod-bound workload bearers by TokenReview, but scope
  grants to the exact ServiceAccount incarnation as well as namespace/name: deleting
  and recreating an account of the same name must not inherit its predecessor's
  access. The current `WorkloadPrincipal` records namespace/name and Pod identity,
  **not** SA UID; implementation must extend its verified identity (or make an
  equivalent UID check against Kubernetes), test token/SA replacement, and refuse
  grants that cannot be verified. Never trust a forwarded principal header.
- Operator authentication remains separate; operator history reads and management
  do not acquire an SA grant by accident. App and history service identities used
  for ingestion/projecting folds are service-to-service privileges, not an all-history
  grant reusable by a hosted agent.

Authorization lives with the history's Session record and read API, in the History
Service. Do not broaden its reader allowlist wholesale: a listed reader reads every
Session. Add narrowly scoped read authorization, separate from the Runner Gateway's
Open/Resume/SubmitCommand and operator policy mutations. A `read` grant does not
imply eventual `send` or `create` authority.

## Sandbox trust boundary

The Sandbox is the security isolation boundary; it is not a choice left open by this
scoped-read proposal. Sessions in one Sandbox share its filesystem, ServiceAccount,
working directories and potentially secrets. Do not assume co-resident Sessions can
be kept separate, regardless of compartment labels or filtered history APIs. Work
requiring incompatible trust or credentials must use separate Sandboxes. A finer
boundary would need a separately designed and reviewed isolation mechanism inside
the Sandbox, which is not planned here.

For this proposed scoped-read policy, assign a Sandbox trust domain before opening its
first classified Session and refuse incompatible Opens from _any_ caller, including
direct Runner Gateway callers and races on different gateway replicas. A Sandbox may
contain multiple Sessions within that domain, but matching compartments alone do not
prove that differently privileged work can safely share credentials or files.
Classification limits external history reads; it does not confer intra-Sandbox isolation.

Do not expose scoped SA reads as a confidentiality guarantee until the admission
boundary is enforced. Existing mixed or unknown co-resident Sessions remain
operator-only until reviewed/reclassified or split into separately isolated
Sandboxes; never silently coalesce their audiences. Deleting the Sandbox does not
delete the durable compartment assignment or enlarge the historical read set.

## Read and revocation behavior

List/discovery, direct Event/history fetch, evidence/frame lookup, retained follow,
and (if later implemented) folded reads all check the **same** scope. Filter before
pagination and totals; an unauthorized ID responds as not found, without existence,
cursor, native payload, or count leaks. Classify and audit changes atomically in the
authoritative store. A reclassification that broadens access to prior history is an
explicit, auditable operator action, not a preset update.

Revocation takes effect on new requests and reconnects; active streams must stop
before delivering further unauthorized Events. Use a durable policy version/epoch or
per-entry authorization check rather than process-local cached grants or a one-time
stream-open check. A revoked reader cannot undo bytes already delivered; the audit
must record assignments, widening and revocation. History lag may be visible to an
authorized reader but must not make a fold claim to cover Events beyond its cursor.

## Implementation readiness and acceptance

Before granting SA reads, land and verify: (1) the independent durable raw history
and one-way import of existing public IDs/Events; (2) Sandbox trust-domain enforcement;
(3) verified SA-incarnation identity; (4) operator-managed grants/classification;
(5) removal of the app's current direct SA transcript-read bypass. Test a director SA
reading explicitly granted compartments but not unrelated histories, and an SA with
no grants discovering none. Cover deleted Sandboxes, same-name replacement, legacy
unclassified Sessions, raw/native paths, list pagination, two history replicas,
reclassification, and revocation while following a stream.

Open design reviews for this PR: confirm UID-pinned grant semantics (including how
TokenReview conveys the ServiceAccount UID) and whether exactly-one compartment plus
no exact-ID exceptions on day one matches the intended delegation workflows. These
questions must be decided before an implementation is marked ready to expose reads.
