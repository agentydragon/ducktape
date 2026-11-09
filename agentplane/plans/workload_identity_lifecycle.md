# Workload identity lifecycle

Status: **draft under review, 2026-11-09.** Decision node
[`WORKLOAD_IDENTITY_LIFECYCLE_DESIGN`](task_dag.md#workload_identity_lifecycle_design--choose-shared-identity-and-svid-management);
implementation is `WORKLOAD_AUTH_CUTOVER`. Background:
[identities and Kubernetes accounts](identity_and_kubernetes_accounts.md) § Workload authentication,
[workload authentication](../docs/workload_authentication.md).

## Question for review

Pick how every workload caller gets its SVID, so the proxy's `kubernetes_bearer` hop and the
workload bearer projections can be removed. The recommendation is one workload-identity component
that owns issuance and resolution. Owners only ask for credentials: the Sandbox Controller for
incarnations, the Action Service for boxes, and cdk8s for standing workloads. An owner asks by
writing a `WorkloadCredential` object, and Sandboxes use the same record: it replaces
`Sandbox.status.runnerCredential` as what verifiers read. The component mints the credential ID and the URI, requests
the cert-manager `Certificate` from one Agentplane workload CA, rotates it, and writes the status
verifiers trust. Every verifier links its resolver, which reads only `WorkloadCredential`s and never
a Sandbox. The component is a package hosted in the Policy Controller binary. Every alternative
shares one first step, [a dedicated workload CA](#the-step-every-alternative-shares-a-dedicated-workload-ca),
which can be dispatched before the rest is accepted.

## Callers

| Caller                     | Created by                                                                     | Proves its incarnation today                                                      | Policy subject today          | Its request under the recommendation                                                             |
| -------------------------- | ------------------------------------------------------------------------------ | --------------------------------------------------------------------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------ |
| Sandbox Pod                | Sandbox Controller                                                             | Runtime SVID; `status.runnerCredential` binds credential ID to Pod UID            | the Sandbox's own SA          | a `WorkloadCredential` from the Sandbox Controller, kind `environment-runtime`, bound to the Pod |
| KubeVirt VM                | Sandbox Controller; the launcher relay gets the Secret after Kyverno admission | Runtime SVID; accepted VMI UID in status                                          | the Sandbox's own SA          | the same record, bound to the VMI                                                                |
| Firecracker guest          | Sandbox Controller; the launcher serves the Workload API over vsock            | Runtime SVID; Pod UID in status                                                   | the Sandbox's own SA          | the same record, bound to the launcher Pod                                                       |
| Action command/build box   | Action Service: a bare Pod from a `PodTemplate`, owned by the caller's SA      | Pod-bound `agentplane-egress` bearer, TokenReview                                 | the calling Sandbox's own SA  | a `WorkloadCredential` from the Action Service, kind `action-box`, subject the caller            |
| Haku UI                    | Haku: a Deployment in `haku-state`, applied to `haku-sandbox` by Flux          | Pod-bound bearer of SA `haku-ui`                                                  | `haku-sandbox/haku-ui`        | a `WorkloadCredential` rendered by cdk8s, kind `standing`                                        |
| Anki                       | as Haku UI                                                                     | Pod-bound bearer of SA `haku-ui`                                                  | `haku-sandbox/haku-ui`        | its own request, same subject (question 4)                                                       |
| Central sampler            | Flux Deployment                                                                | Its own bearer; acts by [delegation](../docs/delegated_authorization.md), no SVID | the effective caller          | out of scope; delegation names the identity                                                      |
| OpenClaw, external callers | `public_coder` cdk8s; none yet                                                 | None: its Deployment is at 0/0, and its dead relay config selects plaintext       | `public-coder-agent/openclaw` | a standing workload once an instance exists; nothing before that                                 |

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
  admission enforces per owner, by URI kind, must be restated for each new owner.
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

Every SPIFFE verifier trusts `cluster-internal-ca-bundle` today. That bundle is the cluster root,
which the cluster-wide `cluster-internal-ca` ClusterIssuer also signs from. So a leaf with an
Agentplane URI and client auth, requested in any namespace, passes TLS, and the live lookup is the
only check left. Every alternative needs verifiers to stop trusting the cluster root, so this step
does not wait for the decision.

- **Issuer.** A dedicated Agentplane workload CA behind an `agentplane-workload` ClusterIssuer, with
  its key in cert-manager's namespace. It replaces the namespaced `agentplane-runner-ca` Issuer. A
  namespaced Issuer would do for Sandboxes alone, but standing workloads in `haku-sandbox` need a
  cross-namespace issuer. Putting a CA key in `haku-sandbox` would let any Pod Haku authors mount
  it. Choosing the ClusterIssuer now avoids a second `issuerRef` move. The Sandbox Controller's
  `runner_certificate_issuer` setting gains the issuer kind, which is a constant `Issuer` today.
- **Trust.** A trust-manager Bundle carries that CA alone. It becomes the client-trust file of every
  SPIFFE listener: the egress proxy's `SPIFFEAuth` and its `svid-ca` volume
  (`cluster/cdk8s/agentplane/egress.py`), and the services' listeners, which take their CA file from
  `workload_tls.TRUST_BUNDLE_FILE` today. Clients verifying service SVIDs may keep the cluster bundle.
- **Who may request.** A `ValidatingAdmissionPolicy` pairs requester and URI kind for Certificates
  that reference the workload issuer: CEL compares `request.userInfo` with `spec.issuerRef` and
  each `spec.uris` prefix. The Sandbox Controller may request only `environment-runtime` URIs in its
  own namespace, and Flux only the service URIs. Only cert-manager may create `CertificateRequest`s
  for the issuer. Once a component owns issuance, its entry replaces the Sandbox Controller's.

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

Host it in the Policy Controller. That process already owns identity-scoped authority: binding
expiry, and the planned grants, managed ServiceAccounts and identity-owned objects. It already
reaches the subject namespaces (`subject_namespaces`) and creates RoleBindings there with `bind` on
the referenced roles. Certificate create on the workload issuer adds no new class of trust to that
account. Issuance is a reconcile with cert-manager doing the cryptography, so it needs no serving
path or availability of its own. A separate binary would buy a separate blast radius, but there is
nothing to separate it from: whoever writes grants can already grant anything a credential reaches.

### Package and the requester interface

```text
agentplane/workload_identity/api/v1alpha1   WorkloadCredential type (the identity domain of the identity plan's question 16)
agentplane/workload_identity/issuer         the reconciler the Policy Controller's manager registers
agentplane/workload_identity/requester      the one interface Go requesters call
agentplane/workload_identity/resolver       verifier library; replaces the lookups in workload_auth/auth/svid.go and sandboxreader
```

```go
package requester

// Requester declares the credential one owner's workload runs with and reports what is issued.
// The Sandbox Controller and the Action Service call it; cdk8s renders the same object for Flux.
type Requester interface {
	// Ensure creates or updates the owner's WorkloadCredential and returns its current status:
	// pending until cert-manager is Ready, then the credential ID, URI and Secret name to mount.
	// A higher Request.Rotation rotates to a new credential ID; Binding names the incarnation
	// (Pod or VMI UID) the owner accepts for the current ID, or none for a standing workload.
	Ensure(ctx context.Context, request Request) (Credential, error)
	// Revoke deletes the owner's WorkloadCredential; the next request presenting it fails.
	Revoke(ctx context.Context, owner Owner) error
}
```

`Subject` and `Binding` are unions of concrete types. `Subject` is a ServiceAccount until
`IDENTITY_CUTOVER`, then an `Identity` name and UID. `Binding` is a Pod UID, a VMI UID, or none.

### The record

```yaml
apiVersion: agentplane.allegedly.works/v1alpha1
kind: WorkloadCredential
metadata:
  name: <sandbox-uid> # Sandbox Controller; the box Pod name; the Deployment name
  namespace: agentplane-staging
  ownerReferences: [{ kind: Sandbox, uid: <sandbox-uid>, controller: true }]
spec: # the requester's; admission pairs requester with kind
  kind: environment-runtime # environment-runtime | action-box | standing
  subject: { serviceAccount: <name> } # { identity: { name, uid } } after IDENTITY_CUTOVER
  rotation: 3 # bump to rotate
  binding: { podUID: <uid> } # or { vmiUID: <uid> }; absent for standing
status: # the component's only
  credentialID: 9f2c... # fresh per generation
  uri: spiffe://agentplane/environment-runtime/agentplane-staging/<sandbox-uid>/9f2c...
  secretName: svid-<name>-9f2c... # svid-<name> for standing, which a Flux Deployment must name
  acceptedBinding: { podUID: <uid> } # copied once the Certificate for credentialID is Ready
```

The URI is `spiffe://agentplane/<kind>/<ns>/<name>/<credential-id>`. The Sandbox Controller names
its request after the Sandbox UID, so Sandbox URIs keep today's form and the Runtime and Firecracker
parsers are unchanged. The path never names where the subject lives. Under every
[architecture direction](architecture_overview.md#four-directions), the request, its Certificate,
its Secret and the workload stay in the workload cluster. Only the subject reference crosses to
wherever `Identity` lives, resolved by name and UID through the shared reader.

**How a verifier resolves a peer.** The identity is never in the certificate. The URI names a
record, and the record names the subject.

1. The TLS listener verifies the chain against the workload CA bundle and the client-auth usage,
   and hands the resolver the verified leaf.
2. The leaf's URI SAN must parse as `spiffe://agentplane/<kind>/<ns>/<name>/<credential-id>`.
3. `<ns>` and `<name>` name the `WorkloadCredential`, and `<ns>` must be one of the verifier's
   allowed namespaces.
4. The resolver makes one uncached GET of that record. A missing or deleting record is refused.
5. Four checks follow. `status.credentialID` must equal the URI's last segment. `status.uri` must
   equal the whole URI. `spec.kind` must equal the kind segment. A kind that needs a binding must
   have `status.acceptedBinding`.
6. `spec.subject` is the subject. Until `IDENTITY_CUTOVER` it is a ServiceAccount, whose UID the
   resolver reads. After that it is an `Identity` name and UID, which the shared policy reader must
   find live with that UID.
7. The caller may also offer a Pod-bound token: the Kubernetes-audience token a sidecar forwards
   for `projectedWorkloadToken`. Its TokenReview Pod UID must then equal `status.acceptedBinding`.

Each request, including one inherited through a tunnel, repeats steps 4 to 7. Until the cutover,
the resolver returns today's `Principal`, with the binding as the provenance Pod, so no service
authorization changes.

Sandbox semantics carry over. The request is owned by the Sandbox, so it outlives finalization
while Runtime seals history, and it goes when the Sandbox does. A VMI replacement bumps `rotation`, which
rotates the ID. Clearing `binding` revokes. The Sandbox Controller copies `spec.identity` into
`spec.subject`. Because `spec.identity` changes only while nothing runs, no request in flight sees
two subjects. Whether that change also rotates the credential is question 8. This replaces the
identity plan's assumption that verifiers read `spec.identity` from the live Sandbox.

### Admission

On top of the shared CA step:

- **Gates requests.** A `ValidatingAdmissionPolicy` shipped with the CRD pairs requester and kind.
  The Sandbox Controller may write only `environment-runtime`, owned by a Sandbox whose UID is the
  name. The Action Service may write only `action-box`, owned by a Pod of that name. Flux may write
  only `standing`. The status subresource belongs to the Policy Controller alone.
- **Gates Certificates.** The shared step's Certificate policy swaps its Sandbox Controller entry
  for the Policy Controller, for all three kinds. The Sandbox Controller loses Certificate create.

### Standing workloads

Haku UI and Anki are Haku-authored Deployments with replicas, not controller-owned incarnations.
cdk8s renders a `standing` request per Deployment into `haku-sandbox`. Haku's deployer Role cannot
write it. The component issues the Certificate with a stable Secret name, and Haku's Deployment
mounts that Secret into its sidecar, as it mounts the token projection today. Any Pod Haku creates
there could mount it, which is the reach the `haku-ui` ServiceAccount already gives. For a
Deployment, the incarnation is the credential generation. Every replica presents the same SVID, and
no Pod is proven. Revocation is a Flux prune. Rotation is a `rotation` bump in cdk8s.

### What moves out of the Sandbox Controller

This is migration step 2 and the size of the reshuffle:

| Today in the Sandbox Controller                                                                                                                                                                 | Moves to                                    |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------- |
| Minting credential IDs (`newRunnerCredentialID`) and composing the URI (`runnerCertificateURI`)                                                                                                 | `issuer`                                    |
| Building, creating, spec-matching and usage-upgrading `runner-<id>` Certificates (`ensureRunnerCertificate`, `runnerCertificateObject`); the `update` RBAC the upgrade needs is fixed in #15089 | `issuer`                                    |
| Waiting for Ready and validating the issued Secret; the `Certificate*` and `CertificateSecret*` conditions                                                                                      | `issuer`, surfaced as request conditions    |
| Deleting superseded generations' Certificates and Secrets (`deleteOldRunnerCertificates`), and that finalizer step                                                                              | `issuer`; garbage collection of the request |
| `status.runnerCredential.id` and `acceptedId`                                                                                                                                                   | `WorkloadCredential.status`                 |
| The `runner_certificate_issuer` setting; RBAC for Certificate create and delete and Secret delete                                                                                               | Policy Controller settings and RBAC         |
| Verifier lookups: `sandboxreader.FindRunnerCredential` and `RunnerCredentialAccount`, and the Sandbox read in `workload_auth/auth/svid.go`                                                      | `resolver`; verifiers drop Sandbox reads    |

What stays with the Sandbox Controller is deciding when an incarnation needs a new generation and
which Pod or VMI it accepts. It also keeps labelling and mounting: `withRunnerTLS`,
`withRunnerTLSDisk`, the credential label and the VM annotations. The launcher relay's Kyverno
admission reads the request's accepted binding instead of Sandbox status.

## Migration and rollback

| Step                                                                                              | Mixed mode                | Proven in k3s before staging                                                                                                     | Rollback                                             |
| ------------------------------------------------------------------------------------------------- | ------------------------- | -------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| 1. The shared workload CA: ClusterIssuer, trust Bundle, Certificate admission policy              | Bearer still on           | Sandbox SVID route on the narrowed trust; a `cluster-internal-ca` leaf with a live Sandbox's URI is refused                      | Revert the pins                                      |
| 2. CRD, `issuer` in the Policy Controller, `resolver`; the Sandbox Controller becomes a requester | as above                  | Sandbox SVID route on all three runtimes through the request; VMI replacement rotates; deleting a Sandbox after its seal revokes | Revert the pins                                      |
| 3. Action boxes request `action-box`; the box sidecar selects SPIFFE                              | Proxy still allows bearer | Box egress and rules route by SVID; dispose revokes on the next request; caller A's box cannot act as B                          | Revert the Action Service image and templates        |
| 4. `standing` requests; `haku-state` switches its sidecars                                        | as above                  | A generic standing Deployment with two replicas authenticates, and its prune revokes                                             | Revert the `haku-state` commit; the request is inert |
| 5. Remove the bearer hop                                                                          | none                      | Nothing projects the `agentplane-egress` hop audience                                                                            | Revert; no caller depends on it                      |

Steps 1 and 2 have one precondition: zero running Sandboxes. Both admission policies are
`ValidatingAdmissionPolicy` objects, which envtest evaluates with the cert-manager CRDs installed.
Step 1's CA move and trust Bundle have no system test, so they reach staging first. That PR says so and checks one Sandbox start and one
refused foreign Certificate there.

Step 5 removes the `CLEANUP(added 2026-11-09)` set from #15065:

- the proxy's `forward.auth` bearer entry (`cluster/cdk8s/agentplane/egress.py`);
- the command-box hop projection (`command_sandbox.py`);
- the sidecar's `KubernetesBearerProxyAuth` and plaintext transport (`egress/sidecar_config.py`,
  `egress/sidecar/config.go`);
- the forward listener's bearer mode and hop verifier (`egress/identity/authentication.go`).

It also deletes or converts `cluster/cdk8s/public_coder/egress.py:63`, which renders both removed
models. That config is dead: the `public-coder-agent` Deployment is scaled to 0/0, and #15089
tombstones it. `kubernetes_bearer` stays on service listeners for backend callers.

**With `IDENTITY_CUTOVER`.** Box creation needs the caller's own ServiceAccount. `stamp` in
`action_service/sandbox/inventory.go` reads it, runs the box as it, and sets the box's owner
reference to it. `SANDBOX_IDENTITY_SPEC` stops creating that account, so step 3 is a technical
prerequisite of that node, recorded in the DAG. In the same change, the box's owner becomes the
caller's `Identity`. Its `kubectl` moves to impersonation with `EGRESS_IDENTITY_RULES`. Until the
cutover, requests name ServiceAccounts and services see unchanged subjects. The cutover PR switches
every requester's `subject` to an identity, and cdk8s renders `haku-ui` as an `Identity`. Steps 4
and 5 may land on either side.

## Open questions

1. **Running Sandboxes during steps 1 and 2: decided 2026-11-09.** None will run, so there is no
   replacement procedure (see the shared step).
2. **Secrets of deleted Certificates.** cert-manager does not own its Secrets here
   (`--enable-certificate-owner-ref` is off). Recommendation: the issuer deletes them by label, as
   the Sandbox Controller does today. A leftover is inert, because its request is gone.
3. **Pod provenance for standing replicas.** Recommendation: none. Decision history and LLM ingress
   metadata name the request and credential ID.
4. **Anki's identity.** Recommendation: its own request now, with subject `haku-ui`. Split the
   identity when its grants diverge.
5. **Admission mechanism.** Recommendation: `ValidatingAdmissionPolicy` for both the request and
   the Certificate gates, since the apiserver evaluates it and envtest covers it. Kyverno stays only
   for the existing launcher-relay admission. approver-policy would need the cluster's default
   approver disabled.
6. **`Identity.status.workloadCredentials`.** The requests are that list, keyed the other way.
   Recommendation: `IDENTITY_KINDS` omits the field.
7. **Off-cluster callers.** A Secret mount does not reach them. Recommendation: design delivery when
   one exists. The request and the resolver stay as here.
8. **Re-owning a Sandbox: rotate or keep the certificate.** The subject lives in the record, so
   re-owning a Sandbox is a `spec.subject` change, and the certificate could stay. The identity
   plan's [§ Changing an environment's identity](identity_and_kubernetes_accounts.md#changing-an-environments-identity)
   rotates the credential ID on the next start instead.
   - **Rotate.** The boundary shows in audit logs as a new SPIFFE ID, at the cost of one reissue
     per re-own.
   - **Keep the certificate.** Simpler, but one SPIFFE ID then spans two identities in decision
     history and service logs.

   The in-flight hazards are covered either way, because the identity changes only while nothing
   runs. Recommendation: rotate, by bumping `rotation` with the subject change. Operator to confirm.

When `WORKLOAD_AUTH_CUTOVER` lands, this content moves into `docs/workload_authentication.md` and
the SPECs of the Policy Controller, Sandbox Controller, egress proxy and Action Service, and this
file is deleted.
