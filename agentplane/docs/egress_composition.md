# Egress policy composition

A Sandbox presents its workload credential to the egress proxy. The proxy resolves its Identity,
reads that subject's live bindings and evaluates the egress rules in their PolicySets.
[Egress contract](../egress/SPEC.md#decision) · [Policy sets](adr_policy_sets.md)

## The shape

- **`PolicySet`** is a named, subject-free list of typed rules; the proxy enforces its `egress`
  rules. A rule belongs to exactly one set (N:1), so the set is the unit of reuse, which is why
  `github-agentydragon-agent` is a set rather than a rule.
- **`EgressCredential`** is its own object, unlike a rule, because a credential is referenced by
  many rules across many sets and has no such N:1 relation to any of them. Naming it is also
  what makes its placeholder unique: the placeholder is `agentplane-credential-<name>`, derived and
  never authored, and detection is namespace-global.
- **`PolicyBinding.spec.policySets`** is an array of set names, in precedence order. A binding may
  name many sets and a set may be named by many bindings: n:m.
- **`PolicyBinding.spec.subject`** names one Identity or namespaced ServiceAccount. A subject may
  hold several independent bindings. All Sandboxes acting as the same Identity use its grants.

A set grants access only through a live binding. The shared policy reader enforces the binding's
expiry even while the Policy Controller is down. [Expiry](../policy_controller/SPEC.md#expiry)

## Worked examples

These three Sandboxes each use a different Identity. Each Identity has one binding here.

```text
docs-writer-h2n4q   subject: docs-writer   policySets: [pypi-readonly]
pr-bot-8fj3d        subject: pr-bot        policySets: [github-agentydragon-agent]
researcher-4m9tz    subject: researcher    policySets: [github-agentydragon-agent, public-internet]
```

"Extra on top" is a longer set list, not a second mechanism:

```yaml
apiVersion: agentplane.allegedly.works/v1alpha1
kind: PolicyBinding
metadata:
  generateName: researcher-
spec:
  subject:
    identity:
      name: researcher
  policySets: [github-agentydragon-agent, public-internet]
```

The credentialed set can appear in either position. A presented credential placeholder selects
which matching rules can authorize its use.

## Opt-in public Internet access

`hosts: ["*"]` admits any destination host, subject to the existing public-address guard.
The generated `public-internet` set is available in both environments, but is absent from
all defaults, presets and standing bindings. An operator must grant it explicitly via a
`PolicyBinding`; merely creating the set grants nothing. No client is opted in automatically.

```yaml
apiVersion: agentplane.allegedly.works/v1alpha1
kind: PolicySet
metadata:
  name: public-internet
spec:
  rules:
    - type: egress
      hosts: ["*"]
```

This is blanket **HTTP(S)** access, not raw TCP/UDP access. It deliberately permits public data
exfiltration and should not be granted to agents handling sensitive material without reviewing
that trade-off. Identity, binding revocation, method/path checks, TLS verification and
DNS/address pinning still apply. Private, loopback, link-local/metadata and other special-purpose
addresses remain blocked (production configures no address exemptions).

A blanket rule cannot set `credentialRef` or `clusterInternal` (admission and runtime validation).
Credential substitution and private backends need separate, destination-scoped rules. A known
credential placeholder presented to an unauthorized destination is refused even when a blanket
rule would otherwise admit the request. Existing placeholder-directed matching means the blanket
set cannot shadow an authorized credentialed rule, regardless of set/binding order.

For requests with no placeholder, the existing first-match ordering still selects the rule's
`clusterInternal` flag. Put explicit internal-host sets ahead of `public-internet` when
those routes are needed; blanket access alone never authorizes a private address.

## Decided: placeholder-directed matching

Selecting the first matching rule used to let a broad credentialless grant shadow a narrower
credentialed grant. Within a binding, the result depended on set order. Across bindings it
depended on the generated binding names.

The proxy now looks for a recognized placeholder across the namespace's credentials. A request
presenting one needs a matching bound rule for that credential, host, method and path. In the
example above, adding `public-internet` leaves the GitHub credential usable. Without a known
placeholder the first matching rule still decides, including whether it permits an internal
destination. [Egress decisions](../egress/SPEC.md#decision)

### Rejected: a priority integer on the binding

Adding `PolicyBinding.spec.priority` and sorting bindings by it would remove the cross-binding
alphabet accident, and is the obvious reach once that accident is visible.

Naming the credential in each authorized rule resolves the overlap without assigning priorities
at grant time. An operator selecting PolicySets only has to choose which credentials to grant.

### Rejected: a profile label selecting Sandboxes

A label stamped on each Sandbox at creation (`agentplane.allegedly.works/profile`) that a binding's
`sandboxSelector` matched gave "profile" a place to live without a new object.

The constraint that killed it: the concept then had no owning object, only a string two unrelated
places agreed on, and it bound a cross-cutting notion (Action approval, Kubernetes access and
other grants key off the same choice) into the egress resource, so the second consumer would extend
that resource or invent a parallel notion. A subject is one named object, never a selector; the
reusable bundle across rule types is a `PolicySet`, and a launch preset only names sets
([principles](principles.md#separate-concepts-mixed-and-matched-no-opaque-bundles)).

## A grant after launch is a new binding

The app calls PolicyService to grant selected sets through a running Sandbox. The new binding's
subject is the Sandbox's current Identity. Other Sandboxes using that Identity share the grant.
The Kubernetes API generates a binding name for each request, so a later grant can expire or be
revoked independently of the launch-time sets. PolicyService checks that the requested sets
exist. A subsequent deletion can leave a missing set, which the proxy reports in `EgressReady`.
[Grant and revoke contract](../policy_controller/SPEC.md#grants)

## Where a binding came from, and what ends it

A grant made through a Sandbox has a Sandbox owner reference. A grant made directly to an
Identity at launch has an Identity owner reference. These lifetimes matter when several Sandboxes
share an Identity. The Sandbox revoke operation also checks the binding's UID and refuses
Flux-managed bindings, which GitOps would restore. Authorization continues independently of the
app's availability. [Revocation timing](../policy_controller/SPEC.md#revocation-timing)

### Rejected: the app renewing a grant like a lease

A short expiry the app extends while it still approves of the grant would give revocation a
heartbeat. It puts the app back into the enforcement path, which is the one property the split
between them exists to protect: with the app down, what a sandbox may reach does not change.

### Rejected: a per-request use counter

"Allow this one call" is the obvious reach once expiry exists, and a counter on the binding is the
obvious way to build it. A count of requests says nothing about what they did — a retry, a redirect
and a paginated read are one intention and three requests — so the counter would expire a grant
mid-operation and call it enforcement. A genuine one-shot needs something that can see the
operation, which is the webhook decision path a rule may later delegate to; nothing is built for it
until a rule needs one.

## Inference experiments

The `inference-experiments` set in staging grants direct Ollama
inference and LiteLLM's existing `cheap-experiments` key. It is separate from
`basic-identity`, which contains Agentplane platform operations. The default selection for a newly created Identity
includes both sets. Launches reusing an Identity keep its existing grants. Standing caller bindings
and static OpenClaw explicitly include `inference-experiments`. Changing the defaults does not rewrite existing grants. Run `agentplane egress rules` for the active
hosts, methods, paths, and credential placeholders; send the returned placeholder
as `Authorization: Bearer ...` through the configured proxy. Override any matching
`NO_PROXY` entry for these experiment calls (`curl --noproxy ''`), particularly
Static OpenClaw's direct LiteLLM conversation exception. The real tokens stay
in the isolated egress-credentials namespaces, not in agent sandboxes.

Ollama uses its authenticated listener at
`http://ollama.ollama.svc.cluster.local:11435`, including `/api/show`, `/api/chat`,
and the OpenAI-compatible inference endpoints. Model pull/create/delete is not
allowed. The GPUs are shared: keep probes bounded and avoid concurrent load tests.
LiteLLM is at `http://litellm.litellm.svc.cluster.local:4000`; its cheap-experiments
key retains the Terraform-managed model allowlist and shared spending budget.
Key-management and admin endpoints are not granted. This access is independent of
the workload-authenticated LLM ingress used for an agent's own conversation.
