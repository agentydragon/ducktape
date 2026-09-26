---
name: cdk8s_builders
description: Design an ergonomic Python cdk8s builder/construct for a Kubernetes object or CRD kind this repo's generator still uses raw (KEDA, Agentplane's own CRDs, cert-manager, kyverno, ServiceMonitor/PodMonitor, agent-sandbox, etc.), matching cdk8s and cdk8s_plus_34's own class-based conventions rather than inventing new ones. Use before writing or extending a provider-specific cdk8s wrapper under cluster/cdk8s/, or when asked to make cdk8s output for some object kind less raw. Assumes cluster/cdk8s/AGENTS.md's three-tier construct table already answered "does a typed construct exist" for the kind in question — this skill is about how to author the wrapper once tiers 1 and 2 don't cover it. Also use when relocating or restructuring an existing wrapper — a file move alone does not satisfy this skill.
---

# cdk8s builder authoring (Python)

Ducktape's cluster generator (`cluster/cdk8s/`) is Python cdk8s: the `cdk8s` core, `cdk8s_plus_34` for stock Kubernetes kinds, `cdk8s_import`-generated bindings for CRDs. This skill is for the point where tier 1 (`cdk8s_plus_34`'s fluent classes) and tier 2 (`cdk8s_plus_34.k8s`'s raw-but-typed structs) don't cover the kind you need, and callers are building a CRD's generated dataclasses raw at more than one call site — that's where a homegrown wrapper earns its place.

`cluster/cdk8s/AGENTS.md` § "Typed constructs over raw dicts" is the standing policy for _whether_ a typed construct already exists. Assume that check is already done (`dir(cdk8s_plus_34.<Thing>)`, then `dir(cdk8s_plus_34.k8s)`, then checking whether this repo already has `cdk8s_import` output for the CRD) before reaching for this skill.

## A file's location is not its shape

Moving a wrapper into the right directory, or colocating it with its `cdk8s_import` bindings, proves the layout and the Bazel/gazelle mechanics — nothing more. It says nothing about whether the code inside actually follows cdk8s-plus's conventions. Check the shape below against the actual classes and functions every time, including on a pure relocation, and including when the move itself required no other changes. A slice chosen specifically because it needs no design decisions only tests the plumbing; it never validates the thing this skill exists for, so don't mistake completing one for having applied this skill.

## The shape to write: a class named after the kind

A resource that becomes its own Kubernetes object is a **class named after the kind**, constructed as `Kind(scope, id, ...)` — never a verb-prefixed function (`add_kind(...)`, `create_kind(...)`). Constructing a cdk8s construct already adds it to the tree; there is no separate "add" step to name. This is cdk8s-plus's own pattern without exception: `Deployment(scope, id, props)`, `Service(scope, id, props)`, `ConfigMap(scope, id, props)` — a class you instantiate, not a function you call.

Where the wrapper is a thin friendliness layer over exactly one resource, subclass the generated CRD binding directly and call `super().__init__(scope, id, metadata=..., spec=...)` from a friendlier `__init__` — this is how cdk8s-plus's own `ConfigMap`/`Secret`/`Namespace` relate to their generated `ApiObject` base, not composition through an internally-held instance.

None of this needs mutable state or a builder pattern. A plain `__init__` (and, for variant constructors, `@classmethod` factories — see below) works identically on top of a frozen, `cdk8s_import`-generated dataclass as it does on top of cdk8s-plus's own hand-written types. "The generated bindings are frozen" is not a reason to fall back to free functions.

Within the class:

- Keyword parameters ≈ the generated `<Kind>Spec`'s fields, same names and types, so a caller reading `__init__`'s signature is reading the spec.
- State this repo's chosen defaults as real Python defaults, and name them as policy in one docstring line ("Our policy: ..."), not silently.
- `None` means "leave the field unset, let the CRD's/operator's own default apply" — never overload it with a real default value.
- Validate invalid combinations by raising `ValueError` at construction time, never by emitting a spec that only fails at `kubectl apply`.

### The generated binding shares the wrapper's name — alias it

`cdk8s_import` does not prefix CRD-level bindings the way tier 2's core-API generator does (`Kube<Kind>`): the generated class for a CRD is named exactly the kind (`ExternalSecret`, `CiliumNetworkPolicy`, ...). cdk8s-plus avoids this collision for stock kinds because its fluent class and the generated struct never share a name (`Deployment` vs. `KubeDeployment`); a CRD wrapper doesn't have that luxury. Import the generated class under an alias and give the wrapper the clean name: `from <generated_module> import <Kind> as _<Kind>`, then `class <Kind>(_<Kind>): ...`.

## Reference the construct, not its name

Any field shaped like `{name: str, namespace?: str}` that points at another object this same codebase also builds should take the built construct, not a bare string — write a small function (or classmethod) that extracts `.name`/`.metadata.namespace` off it. This is the direct equivalent of cdk8s-plus's own `Service(selector=deployment)` and `Role.from_role_name(...)`: a rename becomes a Python error instead of a silent drift between string constants in two files.

Reserve a bare string parameter for a reference to something genuinely outside this synth (a Secret another team owns, an upstream image digest) — cdk8s-plus itself reserves `.from_*_name(...)` factories for exactly that case, never for something the same chart also constructs.

## Group variant constructors under one type

Where a value can be built several different ways — several sources for the same spec field, several shapes of the same rule — that's **one class, several named `@classmethod` factories**, never a scatter of independently-named top-level functions. This is cdk8s-plus's own pattern for exactly this shape: `Volume.from_config_map(...)`, `.from_secret(...)`, `.from_empty_dir(...)`; `EnvValue.from_value(...)`, `.from_secret_value(...)`, `.from_field_ref(...)` — one type, many named doors in. Two functions that return the same struct type with only a discriminant field differing (a "kind" flag, an enum value) are a tell that they belong under one class as two factories, not two unrelated functions.

Check whether this repo already has a class doing this for another CRD and match its granularity rather than inventing a different one.

## Keep two-sided bookkeeping in one place

`cdk8s_plus_34`'s `container.mount(path, volume)` exists because a pod's `volumes` list and a container's `volumeMounts` must agree, and hand-typing both invites drift; it's a method on the resource class for exactly this reason. A raw CRD-generated Pod-shaped spec (a `ScaledJob`, a hand-built sandbox template) has the identical problem. Give the wrapper class the same shape: one method that builds both the volume entry and every mount referencing it, keyed by a name each caller only writes once.

### Only when the API is genuinely incremental

`container.mount()` earns its keep because a pod is built across many separate calls over its lifetime — containers and volumes get added one at a time, sometimes long after construction, and the actual aggregation happens later, at synthesis. Most wrappers aren't built that way: every value the object needs arrives in one constructor call, with no caller ever adding to it afterward. A single-shot wrapper wants a single-shot `__init__` that builds the whole spec immediately — internal mutable state and a deferred synthesis step are overhead with nothing left to defer. Reach for the incremental shape only when real callers actually build the object piece by piece; don't add it speculatively just because a resource elsewhere in the codebase happens to need it.

Confirming the incremental shape only answers _whether_ to reach for `add_json_patch` on a later call — it says nothing about _what_ to patch with. The patch's value is still bound by the same typed-constructs rule as everything else on this page: build it from the CRD's generated struct for that field, never a raw dict, and check for that struct before assuming one doesn't exist (a well-typed, fixed-shape schema field almost always has one, even for an item appended one at a time rather than supplied all at once in the constructor).

## Derive a shared identity once, don't ask two objects to agree on a string

Where a wrapper builds two objects (or two parts of one object) that must reference each other by a value with no Kubernetes meaning of its own — a workload's own pod-template labels and its own selector, a generated name a sibling resource must also carry — derive that value once from the construct's own identity and write it everywhere it's needed, rather than a user-supplied string or a hand-rolled hash either side could get subtly wrong. cdk8s's own `Names` helper (`Names.to_label_value(construct)`, `.to_dns_label(scope, extra=[...])`) is the exact primitive `cdk8s_plus_34`'s `Workload` base class uses to keep a resource's selector and its own pod template's labels from ever drifting apart, and it reappears wherever cdk8s-plus needs a stable name with no other natural source (aggregated `ClusterRole` label keys, an auto-generated `Volume` name). Two objects that must agree on a value belong on one shared derivation, never on two independently-typed string constants.

## A caller-facing layer earns each function by changing something

Splitting one wrapper into a schema-generic half and a caller-specific half (the
placement question this skill's own repo may answer elsewhere) creates a second
failure mode distinct from the ones above: carrying a function into the caller-specific
half that doesn't actually need to be there. A function belongs in the caller-specific
layer only if it binds something the generic layer doesn't know — a fixed label
convention, a specific set of values, a workaround only one deployment needs. A
function with the same name, the same signature, and the same docstring as the generic
thing it calls adds nothing: it's a re-export wearing a definition. Delete it and have
its callers import the generic name directly — the general rule "import a symbol from
the module that defines it, not one that merely re-exports it" applies with full force
here. This is easy to miss when the split is mechanical (moving CRD-schema code into
one file, keeping every existing caller-facing function name for continuity, even the
ones that turn out to need nothing caller-specific once the generic half exists).
Check every remaining function in the caller-specific half against this test before
calling the split done, not just the ones that looked complicated.

## Escape hatch stays tiered — don't over-build the wrapper

A new wrapper's `__init__` doesn't need every field on day one. An uncovered field takes the CRD's own generated struct as a raw keyword value (never a bespoke dict) and becomes a named keyword the moment a second caller needs it.

### A factory groups schema variance, not one caller's use of the escape hatch

A `@classmethod` factory (above) earns its place on **real, typed variance the CRD
schema itself defines** — an enum-discriminated field, alternate typed sub-structs. A
CRD that leaves a field genuinely untyped (a plugin system's freeform
`metadata: map[string]string`, an opaque values blob) has no schema-level shape to name
a factory after. Wrapping one caller's particular use of that field in a factory doesn't
add cdk8s-plus ergonomics — cdk8s-plus itself never manufactures a shared type for a
field the schema declined to type. That value is exactly what the escape hatch just
above is for: the wrapper's `__init__` takes it as a raw keyword, and the one caller
that needs a specific shape builds it directly, rather than a factory invented to make
an untyped, single-user value look like reusable schema structure.

### Check the generated constructor before reaching for `add_json_patch`

`ApiObject.add_json_patch(...)` is for a value the generated `<Kind>Spec`'s constructor
genuinely cannot express — a field the CRD's schema doesn't surface at all, or a value
only known after the whole tree is synthesized. It is not a stand-in for a field the
generated struct already accepts as a real keyword. Before patching a field in after
construction, check the generated constructor's own signature for it; a field the CRD
schema defines — even with unusual enum casing, a deprecated status, or an
awkward generated name — is almost always already a typed parameter there, and belongs
passed straight through, not bolted on with a patch.

One recurring trap: `cdk8s_import`'s codegen can collapse a schema enum's
duplicate-cased members (`audit`/`Audit`) into one generated member whose wire value
differs in case from the spelling a caller expects. A duplicate-cased `enum:` list in
the schema is the tell that the CRD itself treats the two cases as synonyms — so the
fix is to accept the generated enum's own casing, not bypass the generated field over a
cosmetic mismatch. Reach for `add_json_patch` only once you've confirmed the field
truly isn't reachable from the constructor at all.

## Don't invent a mechanism cdk8s/Kubernetes doesn't already have

`cluster/cdk8s/AGENTS.md`'s boundary rule binds here too: the vocabulary is Kubernetes, cdk8s, Flux and Kustomize objects plus plain Python values — no marker annotation, "provides" declaration, registry, or record type standing in for an object. If a wrapper's design seems to need one of those, stop and ask rather than ship it.

## Reference pointers (Python)

**This repo:**

- `cluster/cdk8s/AGENTS.md` § "Typed constructs over raw dicts" — the tier table and the RBAC-rules subsection, standing policy this skill assumes.
- Worked exemplars: search `cluster/cdk8s/` for other provider wrapper modules already following the shapes above, and imitate whichever one clears them most fully. Don't rely on this skill for which file or directory that is — this layer's own organization is still being reworked, so treat any specific path as likely stale, and treat an existing wrapper's use of free functions instead of classes as something to fix, not a precedent to copy.
- The `cdk8s_import` declaration pattern (macro: `devinfra/js/cdk8s_import.bzl`) for generating a new CRD's Python bindings: search the repo for an existing `cdk8s_import(...)` call and follow its shape rather than assuming where it lives.

**Upstream packages:**

- `cdk8s` (PyPI `cdk8s`) — core API: `App`, `Chart`, `ApiObject`, `ApiObjectMetadata`, `Duration`, `Size`, `Names`, `Testing`, `JsonPatch`.
- `cdk8s_plus_34` (PyPI `cdk8s-plus-34`; ships three Kubernetes minor lines at a time) — tier 1's fluent classes (`Deployment`, `Volume`, `Probe`, `Role`, ...) and tier 2's raw-but-typed layer at `cdk8s_plus_34.k8s`. `dir(cdk8s_plus_34.<Thing>)` / `dir(cdk8s_plus_34.k8s)` in a Python REPL is the fastest existence check.
- Upstream source of truth for exact semantics and defaults: `github.com/cdk8s-team/cdk8s-plus`, `src/*.ts` — the Python package is jsii-generated from this TypeScript, and the TS doc comments carry detail the generated Python stubs don't. The repo has no `main`/`master` branch: each Kubernetes minor gets its own `k8s-<NN>/main` (`k8s-20/main` through `k8s-34/main` today) — clone the branch number matching this repo's pinned `cdk8s_plus_<NN>` (`k8s-34/main` as of writing), not whatever the default checkout gives you. Rendered docs: `cdk8s.io/docs/latest/plus/` (concepts) and `cdk8s.io/docs/latest/reference/` (API reference).
- `cdk8s_import` — this repo's Bazel macro generating real typed Python bindings for any CRD from its installed schema (tier 3). Per `cluster/cdk8s/AGENTS.md` § Ecosystem (checked 2026-09-18): no maintained external jsii/Python library covers any CRD this cluster uses, so pointing `cdk8s_import` at the deployed CRD — not a package search — is always the right move for new CRD coverage.

## Before shipping

- Run `//cluster/cdk8s:test_generate_manifests` — the snapshot is the only pin, and a wrapper that changes rendered output shows up as a diff there.
- If the new kind needs a rule that should hold for every object of that kind in the fleet, add it to `fleet_rules.py` rather than a one-off assertion beside the wrapper.
- If it's a stock-Kubernetes-adjacent kind that could plausibly gain a `cdk8s_plus_34` tier-1/tier-2 entry later, note it in `cluster/cdk8s/AGENTS.md`'s typed-affordances table so the next author doesn't re-derive the same `dir()` search.
