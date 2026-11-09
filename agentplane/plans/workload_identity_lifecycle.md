# Workload identity lifecycle

Status: **draft under review, 2026-11-09.** Decision node
[`WORKLOAD_IDENTITY_LIFECYCLE_DESIGN`](task_dag.md#workload_identity_lifecycle_design--choose-shared-identity-and-svid-management);
implementation is `WORKLOAD_AUTH_CUTOVER`. Background:
[identities and Kubernetes accounts](identity_and_kubernetes_accounts.md) § Workload authentication,
[workload authentication](../docs/workload_authentication.md).

## Question for review

Pick how every non-Sandbox workload caller gets an SVID, so the proxy's `kubernetes_bearer` hop
and the workload bearer projections can be removed. Recommended: **A**, one Agentplane workload CA
behind cert-manager. Each owner requests its own `Certificate`: the Sandbox Controller for
incarnations, the Action Service for boxes, and cdk8s for standing workloads. A Sandbox SVID keeps
binding through Sandbox status. Every other SVID binds through its own `Certificate`, whose
creator-written label names the policy subject. An admission policy makes the URI kind an issuance
boundary, and verifiers trust that CA alone. Also asked: accept one replacement of running
Sandboxes when their issuer moves (question 1), and the edge that puts Action boxes on SVIDs before
`IDENTITY_CUTOVER`.

## Callers

| Caller                     | Created by                                                                     | Proves its incarnation today                                                      | Policy subject today          | Reaches its `Identity` under A                                 |
| -------------------------- | ------------------------------------------------------------------------------ | --------------------------------------------------------------------------------- | ----------------------------- | -------------------------------------------------------------- |
| Sandbox Pod                | Sandbox Controller                                                             | Runtime SVID; `status.runnerCredential` binds credential ID to Pod UID            | the Sandbox's own SA          | `Sandbox.spec.identity` (`SANDBOX_IDENTITY_SPEC`)              |
| KubeVirt VM                | Sandbox Controller; the launcher relay gets the Secret after Kyverno admission | Runtime SVID; accepted VMI UID in status                                          | the Sandbox's own SA          | same                                                           |
| Firecracker guest          | Sandbox Controller; the launcher serves the Workload API over vsock            | Runtime SVID; Pod UID in status                                                   | the Sandbox's own SA          | same                                                           |
| Action command/build box   | Action Service: a bare Pod from a `PodTemplate`, owned by the caller's SA      | Pod-bound `agentplane-egress` bearer, TokenReview                                 | the calling Sandbox's own SA  | the caller's identity, labelled on the box's `Certificate`     |
| Haku UI                    | Haku: a Deployment in `haku-state`, applied to `haku-sandbox` by Flux          | Pod-bound bearer of SA `haku-ui`                                                  | `haku-sandbox/haku-ui`        | a cdk8s-rendered `Certificate` naming identity `haku-ui`       |
| Anki                       | as Haku UI                                                                     | Pod-bound bearer of SA `haku-ui`                                                  | `haku-sandbox/haku-ui`        | its own `Certificate`, same identity (question 4)              |
| Central sampler            | Flux Deployment                                                                | Its own bearer; acts by [delegation](../docs/delegated_authorization.md), no SVID | the effective caller          | out of scope; delegation names the identity                    |
| OpenClaw, external callers | `public_coder` cdk8s; none yet                                                 | Relay config selects plaintext plus bearer, which the TLS-only listener refuses   | `public-coder-agent/openclaw` | a standing workload when an instance exists; no surface before |

## Alternatives

| Criterion          | A. Owners request Certificates from one CA                                                          | B. Agentplane issuing service                                     | C. SPIRE, Kubernetes attestation                                          | D. Bearer kept for non-Sandbox callers               | E. cert-manager `csi-driver-spiffe`                |
| ------------------ | --------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- | ------------------------------------------------------------------------- | ---------------------------------------------------- | -------------------------------------------------- |
| Issuer, enrollment | cert-manager; each owner's RBAC plus an admission gate per URI kind                                 | A service holding the CA key or the only Certificate-create grant | SPIRE server; owners write registration entries                           | cert-manager for Sandboxes; TokenReview for the rest | cert-manager; the Pod's SA token enrolls           |
| Binding to subject | Sandbox status for Sandboxes; the Certificate's subject label for the rest, set only by its creator | The service's record, written on owners' calls                    | Registration selectors (ns, SA, Pod UID, labels)                          | SA to identity mapping for bearer callers            | URI is `ns/<ns>/sa/<sa>`                           |
| Delivery, rotation | Secret mount, or the Firecracker Workload API; cert-manager renews                                  | The service writes Secrets and renews                             | Workload API socket through the SPIRE CSI driver                          | unchanged                                            | CSI volume per Pod                                 |
| Revocation         | Delete the record: Sandbox status, or the Certificate; checked live per request                     | Delete the service record; checked live                           | Delete the entry; SVID expiry bounds it unless verifiers check live state | TokenReview, cached up to 30s                        | Pod deletion; SA deletion                          |
| Owner gone         | Owner reference on the Certificate; the owner deletes it first                                      | The service must watch every owner kind                           | spire-controller-manager prunes entries                                   | unchanged                                            | Pod lifetime                                       |
| Verifier change    | URI parser and resolver gain kinds; Certificate `get` in allowed namespaces                         | Resolver calls the service on every request                       | New trust bundle source; the live-state check stays                       | Both modes stay in the proxy and every service       | Resolver keyed on SA                               |
| Footprint          | Move the CA to a ClusterIssuer; one trust Bundle; one Kyverno policy                                | A new availability-critical service on every start and renewal    | Server, datastore, agent DaemonSet on every node, CSI driver, second CA   | none new                                             | CSI driver; default approver disabled cluster-wide |
| Bets on            | Every caller has an owner that knows its subject when it creates it                                 | Issuance policy outgrowing an admission rule                      | Agentplane needing attestation of processes nobody provisions             | Non-Sandbox callers keeping a ServiceAccount         | SA as the workload subject                         |

**What kills the others.**

- **B** adds nothing that A's admission gate lacks. A standing workload still has to prove who it is
  when it enrolls. That proof is a Pod-bound bearer, which moves the TokenReview mode rather than
  removing it, unless Flux pre-registers the workload. Pre-registration is A's Flux-rendered
  `Certificate` behind a second API. B also puts a new service on every Sandbox start and every
  renewal, a credential issuer that `AGENTS.md` admits only as a deliberate design.
- **C** attests only Pods. KubeVirt and Firecracker guests already get their SVID from a launcher
  that reads a projected Secret, so two of three Sandbox runtimes keep today's delivery. The
  per-incarnation acceptance in Sandbox status (VMI acceptance, sealing after deletion) has no SPIRE
  equivalent, so verifiers keep the live check. SPIRE would then add a second CA and a node agent on
  every node, and remove nothing.
- **D** breaks at `SANDBOX_IDENTITY_SPEC`. A command box runs as its caller's Sandbox
  ServiceAccount (`action_service/sandbox/inventory.go`, `stamp`), and that node removes the
  account. Keeping bearers then needs a ServiceAccount per identity, contrary to "no agent is
  provisioned an account by default". It also needs an SA-to-identity map, the
  `status.workloadCredentials.serviceAccount` entry the identity plan sketches, which is a second
  subject model. D stays viable for Haku UI and Anki alone, as the fallback if question 1 is refused.
- **E** issues `spiffe://<td>/ns/<ns>/sa/<sa>`, making the ServiceAccount the workload's identity
  again. After `SANDBOX_IDENTITY_SPEC` every agent Pod runs on the default account and would share
  one SVID. E also requires disabling cert-manager's default approver for the whole cluster.

## Recommendation: A

### Trust and issuance

Today every SPIFFE verifier trusts `cluster-internal-ca-bundle` (`cluster/cdk8s/agentplane/egress.py`
`svid-ca`, `workload_tls.TRUST_BUNDLE_FILE`). That bundle is the cluster root. The cluster-wide
`cluster-internal-ca` ClusterIssuer also signs from it. So a leaf with an Agentplane URI and client
auth, requested in any namespace, passes TLS, and the live lookup is then the only check. A
Sandbox's credential ID can be read from its status. A therefore has two parts:

- **One workload CA.** The runner CA moves to cert-manager's namespace behind an
  `agentplane-workload` ClusterIssuer, because Haku's workloads live in `haku-sandbox`. A namespaced
  Issuer there would need the CA key in a namespace whose Pods Haku authors. A trust-manager
  Bundle carries that CA's certificate alone, and every SPIFFE listener's trust file points at it.
  Server verification of service SVIDs may keep the cluster bundle.
- **Kind is an issuance boundary.** A Kyverno policy on `Certificate` create and update referencing
  that issuer pairs requester and kind. The Sandbox Controller may request `environment-runtime`,
  the Action Service `action-box`, and Flux `standing` and the service URIs. The URI namespace must
  equal the object's namespace, and the name must be `svid-<credential-id>` for the two new kinds.
  Only cert-manager may create `CertificateRequest`s for the issuer. Neither controller holds
  `update` or `patch` on Certificates, so a subject label is fixed at creation.

### URIs and records

```text
spiffe://agentplane/environment-runtime/<ns>/<sandbox-uid>/<credential-id>   record: Sandbox status (unchanged)
spiffe://agentplane/action-box/<ns>/<pod-name>/<credential-id>               record: Certificate svid-<credential-id>
spiffe://agentplane/standing/<ns>/<workload-name>/<credential-id>            record: Certificate svid-<credential-id>
```

The path never names where the subject is stored. Under every [architecture direction](architecture_overview.md#four-directions),
Certificates, Secrets and workload Pods stay in the workload cluster. Only the subject reference
crosses to wherever `Identity` lives, and it is resolved by name and UID through the shared reader.

For the two new kinds, the resolver does an uncached `get` of `svid-<credential-id>` and checks
four things. The object is not deleting. Its issuer is the workload issuer. Its `spec.uris` is
exactly the presented URI. Its kind label matches the path. For `action-box`, it also needs a
controller owner reference to a Pod with the path's name, and that reference supplies the
provenance Pod name and UID. The subject is the creator-written label: a ServiceAccount
until `IDENTITY_CUTOVER`, then an identity name and UID. The resolver returns today's
`Principal` until then, so no service authorization changes. Sandboxes stay on the status record,
because a Certificate cannot express VMI acceptance or sealing after deletion.

### Lifecycle per kind

- **Action box.** The Action Service creates the Pod with a Secret volume `svid-<credential-id>`,
  which the kubelet waits for. Then it creates the Certificate owned by that Pod and labelled with
  the caller's subject. Dispose deletes the Certificate first, so the next request fails, then the
  Secret and the Pod. Pod garbage collection is the backstop. After `IDENTITY_CUTOVER` the box runs
  on the default ServiceAccount, owned by the caller's `Identity` instead of its ServiceAccount, so
  deleting an identity removes its boxes.
- **Sandbox.** Unchanged, apart from the issuer reference.
- **Identity deleted.** The resolver's identity lookup fails on the next request, and the record
  waits for its owner's garbage collection.

### Standing workloads

Haku UI and Anki are Haku-authored Deployments with replicas, not controller-owned incarnations. No
CR is added. The operator's cdk8s renders a `standing` Certificate per Deployment into
`haku-sandbox`, labelled with the subject. Haku's deployer Role cannot create Certificates. Haku's
Deployment mounts the Secret into its sidecar, as it mounts the token projection today. Any Pod Haku
creates in that namespace could mount the Secret, which is the same reach the `haku-ui`
ServiceAccount gives it now. For a Deployment, the incarnation is the credential generation: every
replica presents the same SVID, and no Pod is proven. Revocation is a Flux prune of the Certificate.
Rotation is a new credential ID in cdk8s. cert-manager renews within a generation.

### Verifier changes

- `environment_runtime_transport`: one URI parser returning a typed variant per kind, and the
  workload server's client authorizer accepts all three.
- `workload_auth/auth.SVIDResolver`: dispatch on the variant, and a Certificate reader per allowed
  namespace. The proxy and the services Haku reaches add `haku-sandbox` to their allowed namespaces
  and gain Certificate `get` there.
- Egress `projectedWorkloadToken`: a box's Kubernetes-audience token binds to the record's Pod
  until `EGRESS_IDENTITY_RULES` removes that source. Standing workloads have no Pod binding, and
  none uses one.

## Migration and rollback

| Step                                                         | Mixed mode                     | Proven in k3s before staging                                                                                | Rollback                                                 |
| ------------------------------------------------------------ | ------------------------------ | ----------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| 1. Workload CA, ClusterIssuer, trust Bundle, issuance policy | Bearer still on                | Sandbox SVID route on the narrowed trust; a `cluster-internal-ca` leaf with a live Sandbox's URI is refused | Revert pins; replace running Sandboxes again             |
| 2. Parser, resolver kinds, verifier RBAC                     | No issuer of the new kinds yet | Resolver tests for each kind, including a Certificate whose URI or label disagrees                          | Revert                                                   |
| 3. Action boxes issue SVIDs; box sidecar selects SPIFFE      | Proxy still allows bearer      | Box egress and rules route by SVID; dispose revokes on the next request; caller A's box cannot act as B     | Revert the Action Service image and templates            |
| 4. Standing Certificates; `haku-state` switches its sidecars | as above                       | A generic standing Deployment with two replicas authenticates, and its prune revokes                        | Revert the `haku-state` commit; the Certificate is inert |
| 5. Remove the bearer hop                                     | none                           | Nothing projects the `agentplane-egress` hop audience                                                       | Revert; no caller depends on it                          |

Kyverno and cert-manager objects have no system test, so step 1's policy reaches staging first. Its
PR says so and checks one Sandbox start and one denied foreign Certificate there. Step 5 removes the
`CLEANUP(added 2026-11-09)` set from #15065: the proxy's `forward.auth` bearer entry
(`cluster/cdk8s/agentplane/egress.py`), the command-box hop projection (`command_sandbox.py`), the
sidecar's `KubernetesBearerProxyAuth` and plaintext transport (`egress/sidecar_config.py`,
`egress/sidecar/config.go`), and the forward listener's bearer mode and hop verifier
(`egress/identity/authentication.go`). It also deletes or converts `cluster/cdk8s/public_coder/egress.py`,
which renders both removed models. `kubernetes_bearer` stays on service listeners for backend
callers.

**With `IDENTITY_CUTOVER`.** Step 3 must reach staging before `SANDBOX_IDENTITY_SPEC` does,
because boxes run as their caller's Sandbox ServiceAccount, which that node removes. That is a
technical edge from the box part of `WORKLOAD_AUTH_CUTOVER` to `IDENTITY_CUTOVER`. Until the
cutover, records label a ServiceAccount and services see unchanged subjects. The cutover PR switches
the label to an identity reference on every record writer: the Action Service, and cdk8s, which
renders `haku-ui` as an `Identity`. Steps 4 and 5 may land on either side.

## Open questions

1. **Issuer move and running Sandboxes.** Moving the Sandbox Controller to the ClusterIssuer changes
   `issuerRef` on every runner Certificate. The controller reports that drift as
   `CertificateIdentityConflict`. Recommendation: replace running Sandboxes in step 1, under the
   disposable-state rule, rather than teach the controller to accept one drift. Folding it into
   `IDENTITY_CUTOVER`'s replacement would hold the trust fix until then.
2. **Secrets of pruned Certificates.** cert-manager does not own its Secrets here
   (`--enable-certificate-owner-ref` is off). Recommendation: owners delete them by label, as the
   Sandbox Controller does. A Flux-pruned Secret may linger: it is inert, because its record is gone.
3. **Pod provenance for standing replicas.** Recommendation: none. LLM ingress metadata and decision
   history name the workload and credential ID. Neither caller needs a per-Pod authority.
4. **Anki's identity.** Recommendation: its own Certificate now, labelled `haku-ui`. Split the
   identity when its grants diverge, as a binding change only.
5. **Kyverno or approver-policy.** Recommendation: Kyverno. It already runs, and approver-policy
   needs the cluster's default approver disabled.
6. **`Identity.status.workloadCredentials`.** Under A nothing writes it: Sandboxes resolve through
   status and every other caller through its Certificate. Recommendation: `IDENTITY_KINDS` omits the
   field.
7. **Off-cluster callers.** A Secret mount does not reach them. Recommendation: design delivery when
   one exists. The record and the subject label stay as here.

When `WORKLOAD_AUTH_CUTOVER` lands, this content moves into `docs/workload_authentication.md` and
the Sandbox Controller, egress and Action Service SPECs, and this file is deleted.
