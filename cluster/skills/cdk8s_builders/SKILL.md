---
name: cdk8s_builders
description: Design an ergonomic Python cdk8s builder/construct function for a Kubernetes object or CRD kind this repo's generator still uses raw (KEDA, Agentplane's own CRDs, cert-manager, kyverno, ServiceMonitor/PodMonitor, agent-sandbox, etc.), matching cdk8s and cdk8s_plus_34's own conventions rather than inventing new ones. Use before writing or extending a provider-specific cdk8s wrapper module under cluster/cdk8s/, or when asked to make cdk8s output for some object kind less raw. Assumes cluster/cdk8s/AGENTS.md's three-tier construct table already answered "does a typed construct exist" for the kind in question — this skill is about how to author the wrapper once tiers 1 and 2 don't cover it.
---

# cdk8s builder authoring (Python)

Ducktape's cluster generator (`cluster/cdk8s/`) is Python cdk8s: the `cdk8s` core, `cdk8s_plus_34` for stock Kubernetes kinds, `cdk8s_import`-generated bindings for CRDs. This skill is for the point where tier 1 (`cdk8s_plus_34`'s fluent classes) and tier 2 (`cdk8s_plus_34.k8s`'s raw-but-typed structs) don't cover the kind you need, and callers are building a CRD's generated dataclasses raw at more than one call site — that's where a homegrown wrapper earns its place.

`cluster/cdk8s/AGENTS.md` § "Typed constructs over raw dicts" is the standing policy for _whether_ a typed construct already exists. Assume that check is already done (`dir(cdk8s_plus_34.<Thing>)`, then `dir(cdk8s_plus_34.k8s)`, then checking whether this repo already has `cdk8s_import` output for the CRD) before reaching for this skill.

## Where it lives

One importable module (a subpackage once it outgrows a single file) per provider/CRD family, named after the provider, never a leading-underscore helper beside its first caller — the next file that needs the same CRD should import your module, not reinvent it. The trigger for extracting a shape into shared module scope is the _second_ call site that needs it — anywhere in the tree, not just in the same file — not the third.

The exact placement convention (one flat file per provider, a per-provider subpackage, whether the raw `cdk8s_import` bindings live alongside the wrapper or separately) is being actively revisited in this repo as of this writing. Check `cluster/cdk8s/AGENTS.md` and the current shape of `cluster/cdk8s/` rather than assuming a specific layout — the principle (one importable, named, shared module per provider) is what's stable, not any particular path.

## The shape to write: one constructor function per resource kind

- Keyword parameters ≈ the generated `<Kind>Spec`'s fields, same names and types, so a caller reading the function signature is reading the spec.
- State this repo's chosen defaults as real Python defaults, and name them as policy in one docstring line ("Our policy: ..."), not silently.
- `None` means "leave the field unset, let the CRD's/operator's own default apply" — never overload it with a real default value.
- Validate invalid combinations by raising `ValueError` at construction time, never by emitting a spec that only fails at `kubectl apply`.

## Reference the construct, not its name

Any field shaped like `{name: str, namespace?: str}` that points at another object this same codebase also builds should take the built construct, not a bare string — write a small function that extracts `.name`/`.metadata.namespace` off it. This is the direct equivalent of cdk8s-plus's own `Service(selector=deployment)` and `Role.from_role_name(...)`: a rename becomes a Python error instead of a silent drift between string constants in two files.

Reserve a bare string parameter for a reference to something genuinely outside this synth (a Secret another team owns, an upstream image digest) — cdk8s-plus itself reserves `.from_*_name(...)` factories for exactly that case, never for something the same chart also constructs.

## Name the recurring value fragments

Where the CRD's generated schema has a shape that recurs across rules or fields — a matcher, a port entry, an env-var triad, a resource request/limit pair — give it a small function returning that struct, parameterized on what call sites actually vary and nothing else: a handful of lines, named for what it means, not for its field path. Check whether this repo already has a module doing this for another CRD and match its granularity rather than inventing a different one.

## Keep two-sided bookkeeping in one place

`cdk8s_plus_34`'s `container.mount(path, volume)` exists because a pod's `volumes` list and a container's `volumeMounts` must agree, and hand-typing both invites drift. A raw CRD-generated Pod-shaped spec (a `ScaledJob`, a hand-built sandbox template) has the identical problem with no `.mount()` to lean on. At minimum: one module-level name constant per volume, and one function that builds both the volume entry and every mount referencing it, so the string is written once rather than once per container.

## Escape hatch stays tiered — don't over-build the wrapper

A new wrapper doesn't need every field on day one. An uncovered field takes the CRD's own generated struct as a raw keyword value (never a bespoke dict) and becomes a named keyword the moment a second caller needs it.

## Don't invent a mechanism cdk8s/Kubernetes doesn't already have

`cluster/cdk8s/AGENTS.md`'s boundary rule binds here too: the vocabulary is Kubernetes, cdk8s, Flux and Kustomize objects plus plain Python values — no marker annotation, "provides" declaration, registry, or record type standing in for an object. If a wrapper's design seems to need one of those, stop and ask rather than ship it.

Nor should a wrapper import cdk8s-plus's TypeScript idiom wholesale. cdk8s-plus's own ergonomics come from mutable classes with `add_*`/`.select()` methods accumulating state before synth, which works because its hand-written TS classes wrap a plain construct. The `cdk8s_import`-generated Python CRD bindings are frozen dataclass-shaped constructs with no equivalent mutation API. Matching cdk8s-plus's _conventions_ in this repo means matching its ergonomic principles — reference over name, named factories over inline literals, one primitive plus convenience wrappers, a tiered escape hatch — in small pure functions returning typed spec fragments, the idiom this codebase already uses for that layer. Not re-deriving a stateful builder class the generated bindings don't support.

## Reference pointers (Python)

**This repo:**

- `cluster/cdk8s/AGENTS.md` § "Typed constructs over raw dicts" — the tier table and the RBAC-rules subsection, standing policy this skill assumes.
- Worked exemplars: search `cluster/cdk8s/` for other provider wrapper modules already following the shapes above, and imitate whichever one clears them most fully. Don't rely on this skill for which file or directory that is — this layer's own organization is still being reworked, so treat any specific path as likely stale.
- The `cdk8s_import` declaration pattern (macro: `devinfra/js/cdk8s_import.bzl`) for generating a new CRD's Python bindings: search the repo for an existing `cdk8s_import(...)` call and follow its shape rather than assuming where it lives.

**Upstream packages:**

- `cdk8s` (PyPI `cdk8s`) — core API: `App`, `Chart`, `ApiObject`, `ApiObjectMetadata`, `Duration`, `Size`, `Testing`, `JsonPatch`.
- `cdk8s_plus_34` (PyPI `cdk8s-plus-34`; ships three Kubernetes minor lines at a time) — tier 1's fluent classes (`Deployment`, `Volume`, `Probe`, `Role`, ...) and tier 2's raw-but-typed layer at `cdk8s_plus_34.k8s`. `dir(cdk8s_plus_34.<Thing>)` / `dir(cdk8s_plus_34.k8s)` in a Python REPL is the fastest existence check.
- Upstream source of truth for exact semantics and defaults: `github.com/cdk8s-team/cdk8s-plus`, `master` branch, `src/*.ts` — the Python package is jsii-generated from this TypeScript, and the TS doc comments carry detail the generated Python stubs don't. Rendered docs: `cdk8s.io/docs/latest/plus/` (concepts) and `cdk8s.io/docs/latest/reference/` (API reference).
- `cdk8s_import` — this repo's Bazel macro generating real typed Python bindings for any CRD from its installed schema (tier 3). Per `cluster/cdk8s/AGENTS.md` § Ecosystem (checked 2026-09-18): no maintained external jsii/Python library covers any CRD this cluster uses, so pointing `cdk8s_import` at the deployed CRD — not a package search — is always the right move for new CRD coverage.

## Before shipping

- Run `//cluster/cdk8s:test_generate_manifests` — the snapshot is the only pin, and a wrapper that changes rendered output shows up as a diff there.
- If the new kind needs a rule that should hold for every object of that kind in the fleet, add it to `fleet_rules.py` rather than a one-off assertion beside the wrapper.
- If it's a stock-Kubernetes-adjacent kind that could plausibly gain a `cdk8s_plus_34` tier-1/tier-2 entry later, note it in `cluster/cdk8s/AGENTS.md`'s typed-affordances table so the next author doesn't re-derive the same `dir()` search.
