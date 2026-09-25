# cdk8s adoption: remaining work

Review baseline: `origin/devel` at `eec338cb4e` (2026-09-24). This is a source
audit, not a fresh synthesis, CI result, or live-cluster health report.

The central chart contains 180 Flux Kustomizations. The broad resource conversion,
ArtifactGenerator wiring, two output roots, and removal of redundant single-file
Kustomize wrappers are implemented. They are no longer migration waves.

Adoption is not complete: mixed directories still contain hand-written overlays and
configuration, project-owned deployment packages remain outside that conversion, and
some Python constructs still encode relationships as independent strings or patches.
The remaining YAML has a file-specific treatment in
[the remainder backlog](../docs/cdk8s_remainder.md). Current mechanics and boundaries
live in [the design](../docs/cdk8s.md) and [AGENTS.md](AGENTS.md).

## Recommended order

These are recommendations for subsequent implementation PRs. Updating this plan does
not approve a new abstraction, resource owner, authorization grant, or deployment.

### A. Restore dependency-update ownership

`renovate.json5` scans both `cluster/k8s` and `cluster/generated` for Flux and
Kubernetes YAML. Its custom manager covers Terraform provider pins, not cdk8s Python.
There is no cdk8s regeneration workflow. Updating generated YAML conflicts with its
source and the snapshot gate.

The previously approved direction remains: annotate source pins with `# renovate:`,
teach Renovate to update them, and regenerate on its branches. Exclude generated
outputs from mutation only as source coverage replaces it. Keep repository-built image
tags owned by Flux image automation. Operator/chart versions and CRD schema pins need
coordinated updates where they describe the same deployed API.

Done: an actual dependency update changes the Python source and its generated output,
passes the generation gate, and leaves no independently editable duplicate pin.

### B. Convert useful YAML seams

Start with Grocy's household overlays, then Airlock's typed configuration and the
rotator rosters; Haku CI's shared runner/Pod values is another independent slice.
The remainder backlog names the existing models, semantic hazards, and acceptance
conditions. Authentik blueprints need a separate ownership decision consistent with
`cluster/docs/sso.md`; embedding their text in Python is not completion.

Generate non-secret configuration through the application's existing model where one
exists. Extract a light schema module when that would otherwise import runtime clients.
Keep secret values runtime-only. Keep payloads in their native format when that is the
better authoring interface; cdk8s can own their packaging and references.

Done per slice: one source supplies the duplicated values, the YAML overlay or duplicate
roster is removed, and the final resource/config semantics are checked. Preserve
ConfigMap rollout behavior; serialization changes can change hashes and restart Pods.

### C. Close validation gaps before moving checks

`fleet_rules.add_fleet_rules` has 11 production registration sites, not universal
coverage. `resolved_references` only rejects a reference when the same name exists as
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

### D. Reduce raw construction where it buys relationships

No production `ApiObject(...)` constructors were found in `cluster/cdk8s`; the
remaining low-level code is mostly typed `k8s.Kube*` bindings, Helm values and a small
patch surface. Typed schema bindings are valid cdk8s adoption.

Prefer fluent constructs when they remove independent selectors, names, ports or volume
references. Preserve exact behavior with typed core/CRD bindings when the fluent layer
would require several compensating patches. Treat these as targeted improvements:

- `forgejo/cache.py`: expose the used toleration through `valkey_instance` using the
  imported CRD's type instead of a raw `/spec/tolerations` patch.
- `seaweedfs/s3.py`: use generated structs for grant/access patch values; keep the
  useful Bucket/Identity API and assess its chart-local grant mutation separately.
- `haku/{console,migration}.py`: check the pinned fluent container API before retaining
  positional `containers/0/terminationMessagePolicy` patches.
- Keep the shared typed pod-seccomp patch while the pinned API requires it. Review
  Kyverno's schema-gap patches and Helm's explicit-null patch against their actual
  schemas; do not erase them merely to reduce a count.
- Try one ServiceMonitor helper that takes the actual Service/selector and typed
  endpoints. Preserve differences in auth, labels and timeouts across ntfy, aiquota and
  LiteLLM. A helper earns its place by owning that relationship, not just shortening
  constructor syntax.

Done per slice: fewer separately authored facts or untyped values, no loss of expressible
Kubernetes fields, and a reviewed rendered diff.

### E. Make chart composition consistent in small steps

The existing target is `chart(app, ...values) -> Chart`, with synthesis/writing outside
resource construction and Flux nodes receiving already-built dependencies and values.
`litellm/proxy.py:litellm` still accepts `root`, constructs and writes its workload,
then returns its Flux node. Separate that concrete exception first.

Keep the explicit forward Flux graph. Its 1,648-line entry point is a navigation cost,
not proof that a registry or graph framework is needed. Extract an area only when its
inputs/outputs form a clear boundary. Keep the current small `write_charts` helper
unless a concrete composition/validation requirement justifies a different writer;
callables used immediately by this helper do not by themselves justify a fleet rewrite.

Use an Environment object for repeated deployments (Agentplane, Grocy); constants and
direct parameters remain suitable for a singleton. Use a function for a repeated
resource shape, a Construct when it owns related resources or useful operations, and a
typed schema directly for a one-off. Do not introduce a universal application base
class or a parallel deployment specification.

Done: a workload can be synthesized without building its Flux node or writing to a real
checkout, and new abstractions demonstrate their value on actual callers.

### F. Finish layout only where it simplifies ownership

Keep mixed directories colocated under `cluster/k8s` under the current rule. Move a
whole directory to `cluster/generated` when all its files are generator-owned.
A smaller hand-written tree is useful visibility, not an end in itself.

Cross-root Components remain an open design option, not an approved migration.
The preserved mechanism findings and alternatives are in the remainder backlog.
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
