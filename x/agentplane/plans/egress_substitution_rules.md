# Declared credential substitution rules

Status: **proposed**; nothing implements this. The egress proxy substitutes a credential by
replacing one `placeholder` string wherever a stack of undeclared heuristics happens to find it.
This entry replaces that with a rule that names the parse and the location, and matches a whole
parsed component exactly.

The proxy is <../egress/SPEC.md>; the kind is
<../../../cluster/k8s/agentplane-crds/crd-egresspolicies.yaml>; how the kinds compose into one
decision is <../docs/egress_composition.md>.

## What is wrong with a bare placeholder string

The `EgressPolicy` CRD types `credential.placeholder` as `string` with `minLength: 1` and no
pattern. `agentplane-credential:github-pat` is a convention of the single policy that exists
(<../../../cluster/k8s/agentplane-staging/egress/egresspolicy-github-public.yaml>), not a
constraint the schema carries. What the schema admits is what the proxy must be safe against.

**Substitution is a substring replace over the whole header value.** `swap_placeholder`
(<../egress/policy.py>) calls `str.replace`, so it swaps every occurrence anywhere in the value. A
policy declaring the placeholder `token` turns `Authorization: Bearer my-token-here` into a header
carrying the real PAT spliced into the middle of an unrelated string, and forwards it.

**Detection and substitution are two implementations of one parse.** `contains_placeholder`
answers yes when the placeholder is in the raw value **or** in the decoded `Basic` payload.
`swap_placeholder` tries the raw replacement first and reaches the payload only when the raw
replacement changed nothing. A value carrying the placeholder both raw and inside the payload is
detected once and substituted once — in the raw text alone — and the payload's copy is forwarded.
`_substitute` has the same shape of hole from the other side: when the swap changes nothing it
returns no substitution and the request is allowed anyway, so a placeholder the detector saw
leaves the proxy intact. Neither is likely with today's one long placeholder. Both are failures the
pair's shape permits and one shared parse forecloses.

**The `Basic` path is a second substitution method the schema never declares.** Reaching inside a
base64 `Basic` payload is stated in <../egress/SPEC.md> and absent from the CRD, so an operator
reading the resource they are writing cannot see it, and no rule can ask for it, decline it, or say
which half of `username:password` is the credential.

**Placeholder identity is namespace-global.** The scan for a presented placeholder walks every
`EgressPolicy` in the index, not only those the subject is bound to — deliberately, so a
placeholder the subject was never granted is recognised and refused rather than forwarded. It
follows that a placeholder string is a namespace-wide identifier for a credential, while the schema
treats it as a free string private to the rule that mentions it.

## Requirements

1. **Exact, never substring.** A target declares a parse of one header value; the placeholder must
   equal a whole component that parse yields. No containment test survives.
2. **One parse, shared.** Detection ("does this request present placeholder X") and substitution
   are the same decomposition of the same value, so they cannot drift. The current pair is the bug
   class, so this is a design constraint on the implementation and not only on the schema: a parse
   yields the component and how to rebuild the value around it, and both callers use it.
3. **The value source is declared.** `secretRef` is the one source that exists. It sits in a field
   whose shape admits others later; none are invented here.
4. **Several targets per credential.** One credential is presented in more than one exact location
   — a bearer token to an API and a `Basic` password to git — and both are declared.
5. **Placeholder identity is constrained and namespace-unique**, because detection is
   namespace-global.
6. **Fail closed is preserved.** A presented placeholder that no rule bound to the subject resolves
   still refuses, with `placeholder-unresolved`.

### Exactness narrows detection, and that is safe

Under substring matching a placeholder is caught wherever it appears; under exact matching a
placeholder the sandbox puts somewhere undeclared — a query parameter, a body, a header no target
names — is not detected and is forwarded verbatim. That leaks nothing. The placeholder is inert and
non-secret by construction: the sandbox holds it precisely because it is not the credential. The
refusal exists so that a request asking for a credential cannot quietly proceed without one, not to
keep the placeholder inside the fence.

## Proposed shape

A **credential** is a named object: a value source, the placeholder that stands for it, and the
targets it may be substituted into. A rule references one by name.

```yaml
apiVersion: agentplane.allegedly.works/v1alpha1
kind: EgressCredential
metadata:
  name: github-pat
  namespace: agentplane-staging
spec:
  source:
    secretRef:
      name: agentplane-github-pat
      key: token
  placeholder: agentplane-credential:github-pat
  targets:
    - header: Authorization
      method: schemeToken
      scheme: Bearer
    - header: Authorization
      method: basicPassword
```

```yaml
spec:
  rules:
    - hosts: [api.github.com, github.com, "*.githubusercontent.com"]
      methods: [GET, POST]
      credentialRef:
        name: github-pat
```

**Targets belong to the credential, not the rule.** A target says how a client presents this
credential; hosts, methods and paths say where the credential may go. Declaring the `Basic` target
does not make it fire on the API host — it fires only where a request actually presents the
placeholder in that exact position.

### The parse methods

Each method is a total function from one header value to either a credential component or "this
value is not of that shape", together with how to rebuild the value around a replaced component.
These cover every case below:

| `method`        | Value shape                         | The component                   |
| --------------- | ----------------------------------- | ------------------------------- |
| `wholeValue`    | the value itself                    | the whole value                 |
| `schemeToken`   | `<scheme> <token>`                  | `<token>`                       |
| `basicUsername` | `Basic <base64(username:password)>` | `username`, up to the first `:` |
| `basicPassword` | `Basic <base64(username:password)>` | `password`, after the first `:` |
| `basicWhole`    | `Basic <base64(payload)>`           | the whole decoded payload       |

`schemeToken` carries the scheme it accepts (`Bearer`); the three `basic` methods imply scheme
`Basic`. `basicUsername` and `basicPassword` require a `:` in the decoded payload; one without a
colon is not of their shape, and `basicWhole` is what covers it — a client that sends the
credential as the entire payload. Schemes compare case-insensitively, because clients disagree —
git's own documented form is a lowercase `AUTHORIZATION: bearer`. Header names already compare
case-insensitively. A header sent more than once is parsed per value, and every value whose
component equals the placeholder is rewritten; the others are forwarded untouched.

Adding a method is how a new presentation is supported. Adding a heuristic to an existing one is
not.

### Rejected: matching the encoded form

Declaring that a placeholder may appear base64-encoded, and searching the header value for that
encoded form rather than decoding and comparing a component.

Base64 encodes three bytes to four characters, so a substring's encoding depends on its offset in
the payload — `len(username) + 1` modulo 3 — giving three forms whose leading and trailing
characters carry bits of the neighbouring plaintext. Matching without decoding is therefore three
needles compared against their interiors: substring matching with fuzzy boundaries, which is what
this document exists to remove.

A literal match could not drive a literal replacement in any case. A credential and its placeholder
differ in length, so replacing inside the payload shifts every byte after it, and decode-replace-
re-encode is unavoidable. The search could only ever be the detection half, leaving detection and
substitution to reach the value through different parses — the drift this document forbids.

What it reaches for is already held: naming the parse is what puts the encoding in the vocabulary,
where an operator reads it in the policy. `_basic_payload` is wrong because it is undeclared, not
because it decodes.

### What does not change

Detection stays namespace-global, and a request presenting a placeholder no bound rule resolves
stays a `placeholder-unresolved` refusal. `CONNECT` is still matched on host alone and carries no
substitution; the requests inside the tunnel are decided one by one, and that is where targets
apply. The `DenyReason` vocabulary is unchanged: a placeholder at an undeclared location is not
presented, so it needs no reason of its own.

In code, one credential's substitution becomes several header rewrites rather than one, so
`Substitution` carries a rewrite per header instead of a single header and its values.

## The cases

### Bearer tokens

`Authorization: Bearer <value>`. One target, `method: schemeToken`, `scheme: Bearer`. This is what
the staging policy does today, and the only case the current implementation handles without
reaching for an undeclared path.

### Git push to GitHub over HTTPS

A credential helper sends `Authorization: Basic base64(username:password)`, and so does
`http.extraHeader` when told to — the shape <../../../cpap/gitstore.py> builds by hand. Which half
carries a GitHub PAT is the client's choice and both are in use: the PAT as password under a fixed
username (`x-access-token`), and the PAT as username with the password empty (what
`https://<token>@github.com` sends). So both `basicUsername` and `basicPassword` exist, and the
policy declares the one the sandbox is configured to send.

This is what the current `Basic` path exists for, and it is replaced by declaring one of those two
targets: the placeholder must equal that component entirely, and the value is rebuilt by
re-encoding the pair rather than by editing decoded bytes.

The Agentplane sandbox sends nothing of the sort yet. No placeholder reaches a runner container
today: `agentplane-credential:github-pat` appears in the staging policy and in
<../docs/egress_composition.md> and nowhere else, and the `SandboxTemplate`
(<../../../cluster/k8s/agentplane-staging/app/sandboxtemplate-agentplane-runner.yaml>) sets proxy
and CA environment but no credential placeholder. So the presentation is still ours to choose, and
a third option is open: configure git with `http.extraHeader` carrying `Authorization: Bearer
<placeholder>`, which GitHub accepts for smart HTTP and which needs only the `schemeToken` target.
That choice belongs to the sandbox wiring, not to this schema. The schema must carry the `Basic`
targets regardless, because `gh` and any tool using a credential helper send `Basic` without asking
us.

### The BuildBuddy API key

The key travels two ways, and neither raises a matching question.

Over REST it is an ordinary header whose entire value is the key —
`x-buildbuddy-api-key: <key>`, as <../../../devinfra/ci/bes.py> and
<../../../devinfra/pr_visuals/publisher.py> send it. That is `method: wholeValue` and nothing more
is needed.

Over Bazel's remote APIs it is gRPC metadata: `--remote_header=x-buildbuddy-api-key=<key>` on a
`grpcs://` connection, which is an HTTP/2 header whose entire value is likewise the key. Exactness
costs nothing here either. **The obstacle is transport, not matching.** Nothing in
`x/agentplane/egress/` mentions HTTP/2, HPACK or gRPC; the addon reads `flow.request.headers` and
lets mitmproxy decide what a request is, so whether an intercepted `grpcs://` stream arrives at the
decision at all — with metadata as headers, with trailers intact, over a long-lived
bidirectionally-streaming connection, from a client that must first trust the interception CA — is
unmeasured here.

Two statements in this repository disagree about whether it works elsewhere.
<external_access.md> says the key "rides inside the Bazel gRPC protocol as a remote header rather
than at the HTTP edge, so the fence cannot substitute it".
<../../../cluster/k8s/agents/public-coder-agent/app/deployment.yaml> says the opposite of that
half — "a local Bazel client sends its authenticated gRPC traffic through iron-proxy" — and
locates the real obstacle elsewhere: `bb remote` serializes the key into the command it runs on a
BuildBuddy-hosted runner, which is outside any fence of ours, so a placeholder arrives there
unsubstituted and is rejected. That last part no matching rule can fix; it is why the public coder
holds the real key.

So: **BuildBuddy needs no looser matching.** It needs an experiment establishing whether an
intercepted gRPC request reaches `evaluate` with its metadata as headers, and it needs the
`bb remote` nested-runner path to stay accepted as unfenceable. If the experiment says the request
never arrives, the answer is transport work or a decision to leave the key real — not a matcher
that guesses at bytes the proxy did not parse.

## Placeholder identity

Decided: **identity is an object name, not a well-formed string.** The property that matters is
not that two placeholders differ textually but that one placeholder means one value — detection is
namespace-global, so a placeholder recognised from one policy governs a request granted by another.
A CRD `pattern` constrains one field of one object and can say nothing about agreement between two,
so it cannot deliver that property; a git-side check in `cluster/validation/` covers only policies
that arrive through Flux, and the kind is writable at runtime with `kubectl`. Naming the credential
gets uniqueness from the API server for free, and rules reference it rather than restating it.

Naming it also separates two authorities that are currently one. Today a rule names any Secret key
in the credentials namespace and any header, so writing an `EgressPolicy` is enough to point a
credential anywhere; with credentials as their own kind, RBAC can let one party declare what
credentials exist and another compose policies from them.

The cost is a third kind in a design that deliberately kept rules inline (<../docs/egress_composition.md>).
That decision was about rules, whose N:1 relation to a policy makes the policy the unit of reuse;
a credential is referenced by many rules across many policies and has no such relation.

With identity structural, a `pattern` on `placeholder` stops being load-bearing: under exact
matching a short placeholder is no longer a splice hazard, only a chance of colliding with a real
credential that happens to equal it. Whether the placeholder text should then be authored at all,
or derived from the object's name, is open below.

## Out of scope: anything but a header

Substitution stays header-only. Every credential in the cases above is presented in a header. A
placeholder embedded in a URL therefore stays inert and reaches the upstream unsubstituted, which
is a property to keep: the request fails visibly instead of a URL-borne credential travelling
through logs and referrers. If a case ever forces a non-header location, the parse-method union is
where it enters, as a method that names one, and not as a scan of the request for a string.

## Transition

Migration content is one policy with one placeholder. The contract is still shared with a process
that rolls on its own clock, so the order matters more than the volume.

A policy the running proxy cannot parse does not degrade gracefully: `_parse_policy` raises inside
the informer's list-and-watch cycle, which retries forever with backoff. The proxy keeps serving
from the last picture it had — for a running proxy, the pre-change policy, enforced indefinitely
and silently; for a fresh one, no policy at all, so every request that policy would have admitted
is refused `no-rule`. `/healthz` turns unhealthy three resync periods later. So the new shape must
never reach the API server before the proxy that understands it.

The order, given that nothing exercises the credential path yet:

1. Remove the `github-public` policy from git, so Flux prunes it.
2. Land the CRDs and the proxy together, and let the image roll.
3. Reintroduce the policy and its `EgressCredential` in the new shape.

Between 1 and 3 no policy grants GitHub, which costs nothing while no sandbox presents a
placeholder. A proxy that reads both shapes would remove that window at the price of a shim that
must be deleted afterwards — for a policy nothing uses. Take the window.

The kubeconform schemas under `cluster/schemas/` are generated from the CRDs and pinned by
<../../../cluster/validation/test_agentplane_crd_schemas.py>; they are regenerated in the same
change.

## Open questions

- **A third kind, or an inline credential with a name.** Naming is decided; packaging the name as
  its own `EgressCredential` is what buys uniqueness structurally, and it is the part that adds a
  CRD, RBAC and validator surface. An inline `credential` with a mandatory `name` keeps the object
  count and gets agreement only from a validating admission policy.
- **Authored or derived placeholder text.** Once a credential has a name, the placeholder could be
  `agentplane-credential:<name>` by construction and disappear from the spec, which makes
  disagreement unrepresentable rather than merely detectable. Against: the sandbox's environment
  must carry the same string, and a derived one is harder to grep for.
- **Whether a placeholder must ever be found base64-encoded outside a `Basic` credential** — in a
  JSON body, a query parameter, another envelope. That is a different requirement from any method
  here, and no exact one satisfies it.
- **Whether an intercepted gRPC request reaches `evaluate` at all**, which decides whether the
  BuildBuddy key is a matching question or a transport one.

## What leaving looks like

This entry burns down when the CRDs carry declared targets, the proxy resolves a placeholder
through one shared parse per method, and the staging policy is expressed in the new shape. The
guarantees graduate into <../egress/SPEC.md> and the operator-facing description into the CRDs;
this file goes.
