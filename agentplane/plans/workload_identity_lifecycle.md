# Workload identity lifecycle

Status: **draft under review, 2026-11-09.** Decision node
[`WORKLOAD_IDENTITY_LIFECYCLE_DESIGN`](task_dag.md#workload_identity_lifecycle_design--choose-shared-identity-and-svid-management);
implementation is `WORKLOAD_AUTH_CUTOVER`. Background:
[identities and Kubernetes accounts](identity_and_kubernetes_accounts.md) § Workload authentication,
[workload authentication](../docs/workload_authentication.md).

**Progress.** Step 1 landed (#15104, #15113, #15129, #15131). From step 2, the record, requester and
resolver (#15119) and the issuer (#15127) landed; the cutover is in progress.

## Question for review

Pick how every workload caller gets its SVID, so the proxy's `kubernetes_bearer` hop and the
workload bearer projections can be removed. The recommendation is one workload-identity component
that owns issuance and resolution. Owners only ask for credentials: the Sandbox Controller for
incarnations, the Action Service for boxes, and cdk8s for standing workloads. An owner asks by
writing a `WorkloadCredential` record. Sandboxes use the same record: it replaces
`Sandbox.status.runnerCredential` as what verifiers read. The component mints the credential ID
and the URI `spiffe://agentplane/<namespace>/<name>/<credential-id>`, requests the cert-manager
`Certificate` from one Agentplane workload CA, and writes the status verifiers trust. It never
reads an owner or a Sandbox. Three accounts may write records, and each may name any subject in
its namespace; the real gates stay upstream. Every verifier links the component's resolver, which reads only
`WorkloadCredential`s. The component is a package hosted in the Policy Controller binary. Every
alternative shares one first step, [a dedicated workload CA](#the-step-every-alternative-shares-a-dedicated-workload-ca),
which has landed.

## Callers

| Caller                     | Created by                                                                     | Proves its incarnation today                                                | Policy subject today          | Its request under the recommendation                                                                                 |
| -------------------------- | ------------------------------------------------------------------------------ | --------------------------------------------------------------------------- | ----------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Sandbox Pod                | Sandbox Controller                                                             | Runtime SVID; `status.runnerCredential` binds credential ID to Pod UID      | the Sandbox's own SA          | a `WorkloadCredential` from the Sandbox Controller, named after and owned by the Sandbox, bound to the Pod           |
| KubeVirt VM                | Sandbox Controller; the launcher relay gets the Secret after Kyverno admission | Runtime SVID; accepted VMI UID in status                                    | the Sandbox's own SA          | the same record, bound to the VMI                                                                                    |
| Firecracker guest          | Sandbox Controller; the launcher serves the Workload API over vsock            | Runtime SVID; Pod UID in status                                             | the Sandbox's own SA          | the same record, bound to the launcher Pod                                                                           |
| Action command/build box   | Action Service: a bare Pod from a `PodTemplate`, owned by the caller's SA      | Pod-bound `agentplane-egress` bearer, TokenReview                           | the calling Sandbox's own SA  | a `WorkloadCredential` from the Action Service, named after and owned by the box Pod, subject the caller             |
| Haku UI                    | Haku: a Deployment in `haku-state`, applied to `haku-sandbox` by Flux          | Pod-bound bearer of SA `haku-ui`                                            | `haku-sandbox/haku-ui`        | a `WorkloadCredential` rendered by cdk8s, named after the Deployment, with no owner and no binding                   |
| Anki                       | as Haku UI                                                                     | Pod-bound bearer of SA `haku-ui`                                            | `haku-sandbox/haku-ui`        | its own request, same subject (question 4)                                                                           |
| Central sampler            | Flux Deployment                                                                | Its own bearer; holds no SVID                                               | its own account               | out of scope; nothing acts as a central Session's identity yet (`CENTRAL_SESSION_ACTING_DESIGN` in the task freezer) |
| OpenClaw, external callers | `public_coder` cdk8s; none yet                                                 | None: its Deployment is at 0/0, and its dead relay config selects plaintext | `public-coder-agent/openclaw` | a standing workload once an instance exists; nothing before that                                                     |

## Alternatives

There are two axes. The mechanism axis is cert-manager Certificates, an RPC issuing service,
SPIRE, `csi-driver-spiffe`, or bearers. The placement axis applies within the cert-manager option:
does each owner issue for itself, or does one component issue on the owners' behalf?

| Criterion          | A1. One component issues for owners' requests                                    | A2. Each owner issues for itself                                   | B. RPC issuing service                                      | C. SPIRE, Kubernetes attestation                                     | D. Bearer kept for non-Sandbox callers             | E. cert-manager `csi-driver-spiffe`           |
| ------------------ | -------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ----------------------------------------------------------- | -------------------------------------------------------------------- | -------------------------------------------------- | --------------------------------------------- |
| Issuer, enrollment | cert-manager; only the component creates workload Certificates                   | cert-manager; every owner holds Certificate create                 | A service holding the CA key, or the only Certificate grant | SPIRE server; owners write registration entries                      | cert-manager for Sandboxes; TokenReview for others | cert-manager; the Pod's SA token enrolls      |
| Binding to subject | `WorkloadCredential`: owner-written spec, component-written status               | Sandbox status, plus a creator-written label on other Certificates | The service's records                                       | Registration selectors (ns, SA, Pod UID, labels)                     | An SA-to-identity map for bearer callers           | URI `ns/<ns>/sa/<sa>`                         |
| Delivery, rotation | Secret mount or the Firecracker Workload API; the component rotates IDs          | same; each owner rotates                                           | The service writes Secrets                                  | Workload API socket through the SPIRE CSI driver                     | unchanged                                          | CSI volume per Pod                            |
| Revocation         | Owner clears the binding or deletes the request; checked live                    | Owner-specific records, checked live                               | Service record, checked live                                | Delete the entry; expiry bounds it unless verifiers check live state | TokenReview, cached up to 30s                      | Pod or SA deletion                            |
| Owner gone         | Owner reference on the request; garbage collection removes its Certificates      | Each owner's own cleanup                                           | The service watches every owner kind                        | spire-controller-manager prunes entries                              | unchanged                                          | Pod lifetime                                  |
| Verifier change    | One resolver reading one kind; no Sandbox reads                                  | A resolver per kind, each reading that owner's objects             | A call to the service on every request                      | New bundle source; the live check stays                              | Both modes stay everywhere                         | Resolver keyed on SA                          |
| Footprint          | One CRD, one reconciler in the Policy Controller, CA move, one Bundle, admission | CA move, one Bundle, an admission gate per owner                   | A new serving dependency on every start and renewal         | Server, datastore, agent on every node, CSI driver, a second CA      | none new                                           | CSI driver; default approver off cluster-wide |
| Bets on            | Every caller has an owner that knows its subject when it creates the workload    | Owners stay few and keep their issuance code in step               | Issuance needing an API, not a reconciled object            | Attesting processes nobody provisions                                | Non-Sandbox callers keeping a ServiceAccount       | The SA as the workload subject                |

**What kills the others.**

- **A2** spreads one mechanism over three owners. It means three Certificate builders, three
  rotation rules and three cleanup paths. Every verifier then needs a resolver that reads each
  owner's objects, so the proxy and every service gain Sandbox, Pod and Certificate reads. Each
  owner also holds Certificate create against a CA that signs any URI. That turns every owner into a
  holder of identity-minting authority, and the Sandbox Controller already is one. A boundary that
  admission enforces per owner, by URI prefix, must be restated for each new owner.
- **B** asks by RPC, which Flux cannot make. A standing workload would then enroll with a bearer,
  which keeps TokenReview, or be pre-declared as an object, which is A1 behind a second API. The RPC
  also puts a serving dependency on every Sandbox start. A reconciled object does not.
- **C** attests only Pods. KubeVirt and Firecracker guests already get their SVID from a launcher
  that reads a projected Secret, so two of three Sandbox runtimes keep today's delivery. SPIRE has
  no equivalent of the per-incarnation acceptance, so verifiers keep the live check. It would add a
  second CA and an agent on every node, and remove nothing.
- **D** breaks at `SANDBOX_IDENTITY_SPEC`. A command box runs as its caller's Sandbox ServiceAccount
  (`action_service/sandbox/inventory.go`, `stamp`), and that node removes the account. Bearers would
  then need a ServiceAccount per identity and an SA-to-identity map: a second subject model. D stays
  viable for Haku UI and Anki alone.
- **E** makes the ServiceAccount the workload identity again. After `SANDBOX_IDENTITY_SPEC` every
  agent Pod runs on the default account and would share one SVID. E also needs cert-manager's
  default approver disabled for the whole cluster.

## The step every alternative shares: a dedicated workload CA

Landed in #15104, #15113, #15129 and #15131; the contract is
[workload authentication § The workload CA](../docs/workload_authentication.md#the-workload-ca).
Pod and VM runners' control listeners trust the workload CA alone (#15131). Firecracker's control
listener stays on the cluster root, tracked as `FIRECRACKER_CLIENT_TRUST` in the
[task freezer](task_freezer.md).

**Decided 2026-11-09 (operator):** no Sandboxes run during this cutover, and staging holds zero
Sandboxes now. The `issuerRef` move therefore needs no replacement procedure. A leftover runner
Certificate that reports `CertificateIdentityConflict` is resolved by restarting its Sandbox.

## Recommendation: A1, hosted in the Policy Controller

### The boundary

Three things form the boundary, and changing any of them later touches every requester, every
verifier and stored provenance:

- the `WorkloadCredential` kind;
- the URI form;
- the rule that only the component creates workload Certificates and writes their status.

Where the component runs does not. Requesters see only the object, and verifiers see only the
resolver package and the object. Moving the reconciler to its own binary later means a new
`main.go`, a Deployment and moving its RBAC rules, with no change visible to a requester or a
verifier.

**Decided 2026-11-09 (operator):** host it in the Policy Controller for the time being;
responsibilities may be recut later. That process already owns identity-scoped authority: binding
expiry, and the planned grants, managed ServiceAccounts and identity-owned objects. It already
reaches the subject namespaces (`subject_namespaces`) and creates RoleBindings there with `bind` on
the referenced roles. Certificate create on the workload issuer adds no new class of trust to that
account. Issuance is a reconcile with cert-manager doing the cryptography, so it needs no serving
path or availability of its own. A separate binary would buy a separate blast radius, but there is
nothing to separate it from: whoever writes grants can already grant anything a credential reaches.

### Package and the requester interface

Landed in #15119 and #15127: the packages are described in
[`workload_identity`](../workload_identity/README.md), and the issuer's contract in
[the Policy Controller SPEC](../policy_controller/SPEC.md#workload-credentials). The `resolver`
replaces the lookups in `workload_auth/auth/svid.go` and `sandboxreader` at the cutover.

### The record

Three records, one per kind of owner. The component treats `ownerReferences` as data. It reads the
owner kind as a string and never dereferences the owner.

```yaml
apiVersion: agentplane.allegedly.works/v1alpha1
kind: WorkloadCredential
metadata:
  name: agent-123 # by convention, the owner's name
  namespace: agentplane-staging
  ownerReferences: [{ kind: Sandbox, name: agent-123, uid: <sandbox-uid>, controller: true }]
spec: # the requester's
  subject: { serviceAccount: agent-123 } # { identity: { name, uid } } after IDENTITY_CUTOVER
  binding: { podUID: <pod-uid> } # or { vmiUID: <vmi-uid> }
status: # the Policy Controller's only
  observedGeneration: 2 # a requester reads the rest only for its current spec
  credentialID: 9f2c...
  uri: spiffe://agentplane/agentplane-staging/agent-123/9f2c...
  secretName: svid-agent-123-9f2c...
  acceptedBinding: { podUID: <pod-uid> } # the first binding seen for credentialID
  conditions: [{ type: Ready, status: "True" }] # the Certificate for credentialID is Ready
---
metadata:
  name: sandbox-agent-123-scratch # the box Pod's name
  namespace: agentplane-staging
  ownerReferences: [{ kind: Pod, name: sandbox-agent-123-scratch, uid: <pod-uid>, controller: true }]
spec: { subject: { serviceAccount: agent-123 }, binding: { podUID: <pod-uid> } } # the caller
---
metadata: { name: haku-ui, namespace: haku-sandbox } # rendered by cdk8s; no owner
spec: { subject: { serviceAccount: haku-ui } } # no binding
status: { secretName: svid-haku-ui } # stable: a Flux-applied Deployment must name it
```

**The URI** is `spiffe://agentplane/<namespace>/<name>/<credential-id>`. Namespace and name are the
record's, and by convention the record takes its owner's name. Each part has one job:

- **The credential ID is the revocation.** A stale incarnation's certificate fails the comparison
  with `status.credentialID`.
- **Namespace and name give one uncached GET.** Each verifier also checks the namespace against
  its allowed namespaces before any read.
- **The parts keep logs legible.** An audit line names the namespace and the workload.

There is no kind segment: the record's owner reference says what the workload is. The rejected
form is the opaque `spiffe://agentplane/<credential-id>`. Finding its record needs one of three
things. A per-request label list is what #15023 removed. A cache goes stale. A per-generation
object kind is an object per credential instead of one per workload.

Today's Sandbox URI, `environment-runtime/<ns>/<sandbox-uid>/<credential-id>`, changes form. The
UID was redundant: the credential ID already tells incarnations apart, and step 5 below refuses a
stale certificate after a name is reused. With no Sandboxes running, the change costs nothing.
These consumers switch to the new form:

- the transport's URI builder and parser, and its workload client authorizer;
- Runtime's self-check of namespace and name (`env/runtime`, `env/configuration`);
- the Gateway's expected peer URI;
- the egress SVID resolver;
- the Firecracker Workload API server.

The path never names where the subject lives. Under every
[architecture direction](architecture_overview.md#four-directions), the record, its Certificate,
its Secret and the workload stay in the workload cluster. Only the subject reference crosses to
wherever `Identity` lives, resolved by name and UID through the shared reader.

**How a verifier resolves a peer** landed with the resolver (#15119) and is described in
[workload authentication](../docs/workload_authentication.md#how-a-verifier-resolves-a-peer).
Verifiers adopt it at the cutover, and each request, including one inherited through a tunnel,
resolves again.

**What the binding proves.** TLS proves possession of the key, nothing more. The binding limits how
long a leaked key stays usable; it does not fence who can use the key while it is valid. Three
things defend the key:

- **Key confinement.** The Secret mounts only into Runtime and the sidecar.
- **Per-incarnation IDs.** Replacing an incarnation revokes the old key.
- **Token liveness.** On routes that carry it, the forwarded Pod token must pass TokenReview.

No caller attestation is planned. SPIRE, the attesting option, was rejected because it does not
attest VMs or Firecracker guests.

**Credential IDs.** A credential ID lives exactly as long as the binding it was accepted for. The
first binding after a fresh ID accepts that ID, because the incarnation was started with it. When
`spec.binding` later leaves the accepted binding, the component mints a fresh ID: the binding is
cleared, or replaced by another Pod or VMI UID. Nothing else rotates an ID. A standing workload
has no binding, so its ID never changes, and cert-manager renews its key under the same ID.
Emergency revocation of any record is deleting it. The resolver refuses the old ID on the next
request, and the requester, or Flux, recreates the record with a fresh ID.

**Decided, revisit after landing (operator, 2026-11-09).** Per-incarnation credential IDs are
decided for implementation. The cluster has no CRL or OCSP. A fresh ID per accepted binding is
therefore what makes replacing an incarnation revoke the old key, even a key left in a terminating
Pod, a VM disk or a Firecracker snapshot. The alternative is one stable ID per record plus step 7's
Pod-binding check. That is simpler, but it covers only callers that forward a Pod-bound token.
Revisiting is a possible follow-up once the whole plan has landed, and nothing in the migration
waits on it. Revisit if the ID check proves redundant with binding checks on every route, or if
reissue latency hurts startup.

Sandbox semantics carry over. The record is owned by the Sandbox, so it outlives finalization while
Runtime seals history, and it goes when the Sandbox does. A new Pod or a VMI replacement is a new
binding, so it gets a fresh ID. Clearing `binding` revokes. The Sandbox Controller copies
`spec.identity` into `spec.subject`. Because `spec.identity` changes only while nothing runs, no
request in flight sees two subjects, and the next start rotates the credential (question 8). This
replaces the identity plan's assumption that verifiers read `spec.identity` from the live Sandbox.

### Authorization and admission

**Authorization is coarse (operator, 2026-11-09).** RBAC grants `create`, `update` and `delete` on
`workloadcredentials` to exactly three accounts: the Sandbox Controller, the Action Service and
Flux's applier. Each may name any subject: a ServiceAccount in the record's namespace until the cutover, and
after it any `Identity` in the policy namespace. So each of those accounts can make a
workload act as any identity in its namespace. The real gates stay upstream:

- for a Sandbox, who may set `Sandbox.spec.identity`;
- for a box, the caller the Action Service authenticated;
- for a standing workload, git.

Two facts support the coarse gate. Flux's kustomize-controller already holds `*`, so a narrower
grant to Flux would record intent but not enforce it. Haku cannot author a record, because the
`haku-state` Kustomization impersonates `haku-state-reconciler`, which holds no right on
`WorkloadCredential`s. A narrower requester can be added later without
changing anything here: a `ValidatingAdmissionPolicy` calling the CEL `authorizer` for a custom
`use` verb on the named identity.

The status subresource, its Policy Controller-only RBAC, and the CRD rule that `spec.subject`
changes only with a new `spec.binding` landed in #15119 and #15127. The requesters' grant remains:

```yaml
kind: Role # agentplane-staging; Flux's applier gets the same in haku-sandbox
rules:
  - {
      apiGroups: [agentplane.allegedly.works],
      resources: [workloadcredentials],
      verbs: [get, list, watch, create, update, delete],
    }
# bound to the Sandbox Controller and the Action Service
```

**Certificates** keep one `ValidatingAdmissionPolicy`, from the shared step. Its Policy Controller
entry, for three-segment workload URIs in the Certificate's own namespace, landed in #15127. At the
cutover the Sandbox Controller's entry goes and it loses Certificate create.

**Avoiding the Certificate admission policy (revisit).** Two pure-RBAC forms were considered and
lost:

- **A namespaced CA `Issuer` in a dedicated issuer namespace,** with the External Secrets
  Operator's Kubernetes provider copying each issued Secret into the workload namespace. It lost on
  three counts: an operator poll added to every Sandbox and box start, the operator becoming the
  one holder of every workload key, and an extra controller in renewal.
- **cert-manager approver-policy.** It lost because it needs cert-manager's default approver
  disabled cluster-wide.

Revisit if the External Secrets Operator or cert-manager gains push-based cross-namespace delivery,
or if a namespaced Issuer can sign for Secrets in other namespaces.

### Standing workloads

Haku UI and Anki are Haku-authored Deployments with replicas, not controller-owned incarnations.
cdk8s renders one record per Deployment into `haku-sandbox`, with no owner and no binding. Haku's deployer Role cannot
write it. The component issues the Certificate with a stable Secret name, and Haku's Deployment
mounts that Secret into its sidecar, as it mounts the token projection today. Any Pod Haku creates
there could mount it, which is the reach the `haku-ui` ServiceAccount already gives. For a
Deployment, the incarnation is the record itself. Every replica presents the same SVID, and no Pod
is proven. Revocation is deleting the record, which Flux recreates with a fresh ID, or a Flux prune.

### What moves out of the Sandbox Controller

This is migration step 2 and the size of the reshuffle. **Decided 2026-11-09 (operator):** the
Sandbox Controller loses Certificate create, and this table stands.

| Today in the Sandbox Controller                                                                                                                                                                 | Moves to                                    |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------- |
| Minting credential IDs (`newRunnerCredentialID`) and composing the URI (`runnerCertificateURI`)                                                                                                 | `issuer`                                    |
| Building, creating, spec-matching and usage-upgrading `runner-<id>` Certificates (`ensureRunnerCertificate`, `runnerCertificateObject`); the `update` RBAC the upgrade needs is fixed in #15089 | `issuer`                                    |
| Waiting for Ready and validating the issued Secret; the `Certificate*` and `CertificateSecret*` conditions                                                                                      | `issuer`, surfaced as request conditions    |
| Deleting superseded generations' Certificates and Secrets (`deleteOldRunnerCertificates`), and that finalizer step                                                                              | `issuer`; garbage collection of the request |
| `status.runnerCredential.id` and `acceptedId`                                                                                                                                                   | `WorkloadCredential.status`                 |
| The `runner_certificate_issuer` setting; RBAC for Certificate create and delete and Secret delete                                                                                               | Policy Controller settings and RBAC         |
| Verifier lookups: `sandboxreader.FindRunnerCredential` and `RunnerCredentialAccount`, and the Sandbox read in `workload_auth/auth/svid.go`                                                      | `resolver`; verifiers drop Sandbox reads    |

What stays with the Sandbox Controller is deciding which incarnation it accepts, and nothing more.
It also keeps labelling and mounting: `withRunnerTLS`,
`withRunnerTLSDisk`, the credential label and the VM annotations. The launcher relay's Kyverno
admission reads the request's accepted binding instead of Sandbox status.

## Migration and rollback

| Step                                                                                                           | Mixed mode                | Proven in k3s before staging                                                                                                               | Rollback                                             |
| -------------------------------------------------------------------------------------------------------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------- |
| 1. **Landed.** The shared workload CA: ClusterIssuer, trust Bundle, Certificate admission policy               | Bearer still on           | Sandbox SVID route on the narrowed trust; a `cluster-internal-ca` leaf with a live Sandbox's URI is refused                                | Revert the pins                                      |
| 2. CRD, `issuer` in the Policy Controller, `resolver` (**landed**); the Sandbox Controller becomes a requester | as above                  | Sandbox SVID route on all three runtimes through the request; a VMI replacement gets a fresh ID; deleting a Sandbox after its seal revokes | Revert the pins                                      |
| 3. Action boxes request records; the box sidecar selects SPIFFE                                                | Proxy still allows bearer | Box egress and rules route by SVID; dispose revokes on the next request; caller A's box cannot act as B                                    | Revert the Action Service image and templates        |
| 4. Standing records; `haku-state` switches its sidecars                                                        | as above                  | A generic standing Deployment with two replicas authenticates, and its prune revokes                                                       | Revert the `haku-state` commit; the request is inert |
| 5. Remove the bearer hop                                                                                       | none                      | Nothing projects the `agentplane-egress` hop audience                                                                                      | Revert; no caller depends on it                      |

Steps 1 and 2 have one precondition: zero running Sandboxes. envtest evaluates both the
Certificate `ValidatingAdmissionPolicy`, with the cert-manager CRDs installed, and the record's CRD
transition rule.
Step 1's CA move and trust Bundle have no system test, so they reach staging first. That PR says
so and checks one Sandbox start and one refused foreign Certificate there.

Step 5 removes the `CLEANUP(added 2026-11-09)` set from #15065:

- the proxy's `forward.auth` bearer entry (`cluster/cdk8s/agentplane/egress.py`);
- the command-box hop projection (`command_sandbox.py`);
- the sidecar's `KubernetesBearerProxyAuth` and plaintext transport (`egress/sidecar_config.py`,
  `egress/sidecar/config.go`);
- the forward listener's bearer mode and hop verifier (`egress/identity/authentication.go`).

It also deletes or converts `cluster/cdk8s/public_coder/egress.py:63`, which renders both removed
models. That config is dead: the `public-coder-agent` Deployment is scaled to 0/0, and #15089
tombstones it. `kubernetes_bearer` stays on service listeners for backend callers.

**With `IDENTITY_CUTOVER`; the ordering edge and the owner switch were decided by the operator on
2026-11-09.** Box creation needs the caller's own ServiceAccount. `stamp` in
`action_service/sandbox/inventory.go` reads it, runs the box as it, and sets the box's owner
reference to it. `SANDBOX_IDENTITY_SPEC` stops creating that account, so step 3 is a technical
prerequisite of that node, recorded in the DAG. In the same change, the box's owner becomes the
caller's `Identity`. Its `kubectl` moves to impersonation with `EGRESS_IDENTITY_RULES`. Until the
cutover, requests name ServiceAccounts and services see unchanged subjects. The cutover PR switches
every requester's `subject` to an identity, and cdk8s renders `haku-ui` as an `Identity`. The
resolver then reads `spec.subject.identity` through the shared policy reader, live with its UID, in
the policy namespace rather than the record's own, which matters for `haku-sandbox` records. Steps 4
and 5 may land on either side.

## Open questions

1. **Running Sandboxes during steps 1 and 2: decided 2026-11-09.** None will run, so there is no
   replacement procedure (see the shared step).
2. **Secrets of deleted Certificates: landed in #15127.** The issuer deletes Secrets by the record
   UID it stamps into `secretTemplate.labels`; `--enable-certificate-owner-ref` was rejected because
   it changes behaviour for every Certificate in the cluster.
3. **Pod provenance for standing replicas: decided for now, 2026-11-09.** None. Decision history
   and LLM ingress metadata name the record and its credential ID.
4. **Anki's identity: decided for now, 2026-11-09.** Its own record, with subject `haku-ui`. Split
   the identity when its grants diverge.

   Standing workloads share one non-rotating credential with no Pod provenance, which is weaker
   than the per-incarnation credentials of Sandboxes and boxes. To revisit: bring standing workloads
   up to per-incarnation credentials, or accept the weaker guarantee everywhere.

5. **Admission mechanism: landed.** A `ValidatingAdmissionPolicy` gates Certificates and CRD
   validation rules gate the record. Kyverno stays only for the existing launcher-relay admission.
6. **`Identity.status.workloadCredentials`: decided.** The records are that list, keyed the other
   way, and #15072 merged without the field.
7. **Off-cluster workloads presenting SVIDs.** A Secret mount does not reach them. Recommendation:
   design delivery when one exists. The record and the resolver stay as here. The MCP edge path for
   off-cluster agents is out of this design's scope and unchanged: an OAuth Connection, forwarded to
   the Action Service with the edge's backend credential. Only its Connection-to-Identity binding
   changes, at `IDENTITY_CUTOVER`.
8. **Re-owning a Sandbox: decided 2026-11-09.** The identity changes only while nothing runs, and
   the next start is a new incarnation. Its binding changes, so the credential rotates anyway, and
   the audit boundary of the identity plan's
   [§ Changing an environment's identity](identity_and_kubernetes_accounts.md#changing-an-environments-identity)
   comes at no extra cost.

When `WORKLOAD_AUTH_CUTOVER` lands, this content moves into `docs/workload_authentication.md` and
the SPECs of the Policy Controller, Sandbox Controller, egress proxy and Action Service, and this
file is deleted.
