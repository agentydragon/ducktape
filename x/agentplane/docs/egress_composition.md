# Egress policy composition

How `EgressPolicy`, `EgressBinding` and Sandboxes compose into one decision: the arity between
them, what that makes reusable, and what follows from a rule language with no way to express
exclusion. The proxy that enforces the result is <../egress/SPEC.md>; the kinds are
<../../../cluster/k8s/agentplane-crds/crd-egresspolicies.yaml> and
<../../../cluster/k8s/agentplane-crds/crd-egressbindings.yaml>.

## The shape

- **`EgressPolicy`** is a named, subject-free rule set. `spec.rules` is inline — there is no
  `EgressRule` object, so a rule belongs to exactly one policy (N:1). The policy is therefore the
  unit of reuse, which is why `github-public` is a policy rather than a rule.
- **`EgressCredential`** is its own object, unlike a rule, because a credential is referenced by
  many rules across many policies and has no such N:1 relation to any of them. Naming it is also
  what makes its placeholder unique: the placeholder is `agentplane-credential-<name>`, derived and
  never authored, and detection is namespace-global.
- **`EgressBinding.spec.policies`** is an array of policy names, `minItems: 1` and no upper bound,
  in precedence order. A binding may name many policies and a policy may be named by many
  bindings: n:m.
- **`EgressBinding.spec.subjects`** is an array, `minItems: 1`, each item naming exactly one
  Sandbox. A binding may name many sandboxes and a sandbox may be named by many bindings: n:m.

A policy on its own grants nothing; a binding grants by existing and being unexpired.

### The app writes only 1×N

`EgressInventory.grant()` (<../app/egress.py>) hardcodes a single subject: it writes one `subjects`
entry and hangs an `ownerReference` on that one Sandbox so deleting the sandbox garbage-collects
the grant. Every binding created at runtime is therefore one subject by N policies, and the
schema's multi-subject side is reachable only by a hand-written or Flux-applied binding.
Multi-subject is a seed-time shape, and nothing in the runtime path produces or exercises one.

The object's name comes from `metadata.generateName` with the prefix `{sandbox}-`, so the API
server assigns it and a sandbox can be granted any number of times; a name derived from the
sandbox alone would make every grant after the first a 409. Nothing reads a binding back by name:
the app selects by subject and revokes whatever name it was given.

## Worked examples

Several sandboxes run agents; some need GitHub with a token placeholder, some need more on top.
Each gets one binding:

```text
docs-writer-h2n4q   subjects: [docs-writer]   policies: [pypi-readonly]
pr-bot-8fj3d        subjects: [pr-bot]        policies: [github-public]
researcher-4m9tz    subjects: [researcher]    policies: [github-public, open-internet]
```

"Extra on top" is a longer policy list, not a second mechanism:

```yaml
apiVersion: agentplane.allegedly.works/v1alpha1
kind: EgressBinding
metadata:
  generateName: researcher-
spec:
  subjects:
    - sandbox:
        name: researcher
  policies: [github-public, open-internet]
```

The specific, credentialed policy is listed first. Why that order matters, and why it should not
have to, is the rest of this page.

## Rules are alternatives, and there is no exclusion

A rule carries hosts, methods, paths and an optional credential reference. There is no deny form and no way
to subtract a host from a broader pattern, which is what the `EgressPolicy` CRD means by "Rules
are alternatives; a request matching any one of them is allowed."

So among the rules that match a request, **every one of them would allow it**. Walking bindings,
policies and rules in order and taking the first match does not decide allow versus deny; it
decides only which rule is named in the decision log and which credential, if any, is substituted.

The constraint that follows: where a broad policy overlaps a credentialed one, the overlap cannot
be narrowed away, only ordered around.

## Open question: "any host" is not expressible

`hosts` takes exact names and `*.` suffixes. Nothing spells "any host":

- A bare `*` is refused at admission: the CRD's `hosts` item pattern requires a domain label after
  the optional `*.` prefix.
- Admitted, it would still never match. `host_matches` treats a pattern without a leading `*.` as
  an exact string, so `*` is compared literally against the hostname.
- `*.com` is the closest approximation and is wrong twice over: it misses every other TLD, and a
  `*.` pattern never matches the apex of the suffix it names, so `*.github.com` does not match
  `github.com`.

The natural resolution is to give `"*"` the meaning "any host" — no new field and no new kind, only
the CRD's `hosts` pattern admitting the bare `*` and `host_matches` answering true for it. Nobody
has decided this.

## A broad policy can break a credentialed one

Where the first matching rule decides, adding a broad policy to a subject that already has a
credentialed one can stop the credentialed traffic instead of widening it.

Take `researcher` above with the list reversed, so a credential-less `open-internet` rule
(`hosts: ["*.com"]`) precedes `github-public`. A request to `api.github.com` carrying
`Authorization: Bearer agentplane-credential-github-pat`:

1. `open-internet`'s rule matches first. It names no credential, so nothing is substituted.
2. The scan for presented credentials walks every `EgressCredential` in the namespace and finds
   `github-pat` presented at its `schemeToken` target, which no matching rule names.
3. The request is refused `403` with `x-agentplane-egress: denied; reason=placeholder-unresolved`.

Granting more would make GitHub start failing, which is what placeholder-directed matching below
forecloses.

Inside one binding the author chooses that order. Across bindings nobody does: `subject_bindings`
walks `sorted(index.bindings)`, so the **binding name** decides. A seed named `all-sandboxes-open`
is consulted before `researcher-4m9tz` and produces exactly the refusal above; the same binding
named `zz-open` is consulted after it and the request succeeds. The alphabet is not a policy
decision.

## Decided: placeholder-directed matching

The placeholder a request carries already says which credential the caller wants. The model takes
it as the selector:

- A request presenting a known placeholder is a request to use that credential. It is allowed if
  and only if some rule bound to the subject names **that credential** for that host, method and
  path, and that credential is what gets substituted.
- A request presenting no known placeholder falls back to the first bound rule that matches.

The scan for presented credentials walks every `EgressCredential` in the namespace rather than only
the bound ones, so a placeholder is a namespace-global credential identifier rather than a string
private to the rule that mentions it -- which is also why the placeholder is derived from the
credential's name instead of being written by hand.

What it buys:

- Allow versus deny becomes a union over the matching rules, which is what "rules are alternatives"
  already claims. A broad policy is then safely additive: it can widen a subject's reach and cannot
  take away a credentialed route.
- Cross-binding name order stops being observable, so there is nothing left to prioritise.
- Residual order — two rules naming the same credential, or two naming none — decides only which
  rule the decision log names.

The cost, stated plainly: it changes a line <../egress/SPEC.md> used to state, that the first rule
whose hosts, methods and paths match decides. That is a contract change, acceptable at `v1alpha1`
under `x/` but not free.

### Rejected: a priority integer on the binding

Adding `EgressBinding.spec.priority` and sorting bindings by it would remove the cross-binding
alphabet accident, and is the obvious reach once that accident is visible.

The constraint that killed it: ordering is not load-bearing for authorization. Ranking the inputs
enshrines a workaround for a semantic that can be fixed instead — once allow is a union over
matching rules there is no order left to rank. A priority also has to be set correctly at every
grant, including by the app's create form, which has no basis for choosing a number; an operator
picking policies is not choosing a precedence.

The field remains available later if a real case needs one. Nothing here depends on its absence.

## A grant after launch is a new binding

Granting a policy to an already-running sandbox creates a new binding naming just that sandbox,
rather than appending the policy to a binding the sandbox already has.

`expiresAt` is per binding. A time-limited grant appended to a binding that also carries the
launch-time picks would put its expiry on all of them, so at the deadline the sandbox would lose
the policies it was created with. One binding per grant keeps each lifetime its own, and the union
over a subject's bindings composes them. Revocation follows the same seam: deleting one grant's
binding takes back that grant and nothing else.

The app writes it at `POST /sandboxes/{name}/egress`, which refuses a policy name the namespace
does not hold. A dangling name is not corruption — the CRD admits any string and the proxy answers
one that resolves to nothing with `MissingPolicy` — so the refusal is a guard on the typo at the
moment of writing, not a guarantee: a policy deleted after the grant produces the same dangling
name, and the proxy's condition stays the answer to it.
