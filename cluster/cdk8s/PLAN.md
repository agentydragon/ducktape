# cdk8s adoption: remaining work

Adoption is not complete: mixed directories still contain hand-written overlays and
configuration, project-owned deployment packages remain outside that conversion, and
some Python constructs still encode relationships as independent strings or patches.
The remaining YAML has a file-specific treatment in
[the remainder backlog](../docs/cdk8s_remainder.md). Current mechanics and boundaries
live in [the design](../docs/cdk8s.md) and [AGENTS.md](AGENTS.md).

## Recommended order

These are recommendations for subsequent implementation PRs. Updating this plan does
not approve a new abstraction, resource owner, authorization grant, or deployment.

### Deferred: decide image-pin and dependency-update ownership

The pin and update ownership policy is unresolved. Renovate changes are parked until the
image-pin design is decided. The previously proposed `# renovate:` source annotations and
branch regeneration workflow are one possible approach, not an approved direction. Decide how
image pins, source pins, generated output, and update automation fit together before
changing Renovate or adding cdk8s regeneration.

Done: the chosen policy identifies each pin's source of truth, update owner, and generated
output path without creating two writers.

### Convert useful YAML seams

Authentik blueprints need a separate ownership decision consistent with
`cluster/docs/sso.md`; embedding their text in Python is not completion.

Generate non-secret configuration through the application's existing model where one
exists. Extract a light schema module when that would otherwise import runtime clients.
Keep secret values runtime-only. Keep payloads in their native format when that is the
better authoring interface; cdk8s can own their packaging and references.

Done per slice: one source supplies the duplicated values, the YAML overlay or duplicate
roster is removed, and the final resource/config semantics are checked. Preserve
ConfigMap rollout behavior; serialization changes can change hashes and restart Pods.

### Close validation gaps before moving checks

`fleet_rules.add_fleet_rules` is attached explicitly by selected charts; coverage
is not universal. `resolved_references` only rejects a reference when the same name exists as
the other kind in that chart. Unknown names pass; namespace is not part of its lookup.
It does not establish that every Secret or ConfigMap reference resolves.

First implement the bounded ESO store/consumer checks in [TODO.md](TODO.md), using the
existing whole-tree validation boundary. Then assess chart coverage and reference
resolution by namespace, including optional references and controller-created outputs.
Keep authorization grants explicit; observing a consumer must not automatically grant it
access to a store.

Do not infer a Flux readiness dependency from every application reference. Required
CRDs, webhooks, security setup, substitutions and bounded bootstrap jobs differ from
ordinary runtime availability; retry-capable services should not acquire unnecessary
readiness coupling.

Keep final Kustomize, CRD/admission-layering, Helm-rendering and schema checks where they
exercise a real downstream boundary. A Python construction graph cannot prove that an
image transformer, a remote Helm chart, or a hand-written Component renders correctly.
Remove a check only when its actual invariant and coverage are preserved or made
unrepresentable, not because its inputs became generated.

Done: the declared validation scope matches its behavior, and the known ESO wiring
failures are caught without maintaining a second provider inventory.

### Reduce raw construction where it buys relationships

Typed schema bindings are valid cdk8s adoption; converting every `k8s.Kube*` call
or Helm values dictionary is not a completion criterion.

Prefer fluent constructs when they remove independent selectors, names, ports or volume
references. Preserve exact behavior with typed core/CRD bindings when the fluent layer
would require several compensating patches. Treat these as targeted improvements:

- Keep the shared typed pod-seccomp patch while the pinned API requires it. Review
  Helm's explicit-null patch against its actual schema; do not erase it merely to reduce
  a count.

Done per slice: fewer separately authored facts or untyped values, no loss of expressible
Kubernetes fields, and a reviewed rendered diff.

### Make chart composition consistent in small steps

The existing target is `chart(app, ...values) -> Chart`, with synthesis/writing outside
resource construction and Flux nodes receiving already-built dependencies and values.

Keep the explicit forward Flux graph. Its long entry point is a navigation cost, not
proof that a registry or graph framework is needed. Extract an area only when its
inputs/outputs form a clear boundary.

Use an Environment object for repeated deployments (Agentplane, Grocy); constants and
direct parameters remain suitable for a singleton. Use a function for a repeated
resource shape, a Construct when it owns related resources or useful operations, and a
typed schema directly for a one-off. Do not introduce a universal application base
class or a parallel deployment specification.

Done: a workload can be synthesized without building its Flux node or writing to a real
checkout, and new abstractions demonstrate their value on actual callers.

### Finish layout only where it simplifies ownership

Keep mixed directories colocated under `cluster/k8s` under the current rule. Move a
whole directory to `cluster/generated` when all its files are generator-owned.
A smaller hand-written tree is useful visibility, not an end in itself.

Image-pin Components already cross the roots under the
[documented exception](../docs/cdk8s_remainder.md#mixed-directory-layout). Other
hand-written siblings stay colocated; cross-root SOPS composition remains undecided.
Do not create per-app Secret Kustomizations, copy ciphertext, change image automation,
or change resource ownership merely to make a directory qualify as generated.

## Gates and completion

Use `render_diff.py` for conversions claiming unchanged Kubernetes objects, within
its documented scope: no `postBuild` substitution, ciphertext rather than decrypted
Secrets, and incomplete foreign branch-source rendering. Its non-`/**` glob-copy
behavior needs repair before relying on that mechanism (remainder backlog).

App-payload conversions also need semantic checks at their format/runtime boundary.
A changed payload string or ConfigMap hash is an expected resource diff to review, not
an empty render diff to claim. Ownership moves require the live protection/adoption
procedure in AGENTS.md, including storage identities and traffic checks.

Keep generated output committed while it gives reviewers useful diffs. An OCI delivery
pipeline is a separate proposal requiring a concrete benefit.

Delete each backlog item when its acceptance condition lands; retain necessary
constraints beside the owning code/design. Completion means each remaining hand-written
surface has an intentional owner and treatment, shared facts have one source, and
validation covers the real composition boundaries. It does not mean zero YAML, zero
typed low-level constructs, zero patches, or zero integration tests.
