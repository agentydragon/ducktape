# Debundle Module Shape

The goal is a generated tree that reads like a natural JavaScript codebase, not
a mechanically split bundle.

Current module boundaries, names, and paths are evidence, not ground truth.
They may reflect previous architecture work, but they may also be confused,
stale, or mechanically convenient. Infer architecture from decompiled source
behavior: graph edges, call sites, source proximity, ownership patterns, and
repeated internal structure. Use pulled-out modules as a readable lens over the
JavaScript, not as proof that the current taxonomy is correct.

Never accept a directory tree as architectural evidence by itself. If paths and
names make code look like one kind of app, but source bodies and graph
neighborhoods show another structure, prefer the source/graph evidence and
record the mismatch.

## Seams

A good module seam has at least one of:

- stable public surface
- internal references dominating external references
- multiple meaningful consumers
- clear layer or subsystem ownership
- substantial standalone behavior

Member count is not the rule. A single substantial service, state machine, or
component can be a module. A one-line constant with one consumer usually is
not.

## Tiny Modules Are a Smell — Try to Fold Them

A debundle over-splits when the chunker (or atomic-unit granularity) emits a
separate module for what a developer would have written inline in a larger
file. Actively look for small modules and try to fold each into its true owner:
its single real consumer, or a sibling that was clearly the same original
source file.

Judge by LINES OF CODE, not member count. A one-binding module that is a
500-line React component is a perfectly idiomatic file and must be left alone.
The smell is small-LOC standalone files: a 1-3 line accessor, predicate,
constant, or wrapper sitting in its own module.

Fold a small-LOC module when either holds:

- it has a single real consumer (exclude non-semantic re-export catalogs /
  bundle barrels) — fold into that consumer
- it is a co-located helper/style/type of a sibling that was clearly the same
  original source file — use `source_location` adjacency / shared CSS-module
  class prefixes as evidence

Do NOT fold:

- small-LOC modules that are widely-consumed shared primitives (a shared
  constant, a React context, a public predicate used across the app)
- real public-API / service / class boundaries
- anything whose fold would cross a layer boundary or break the realizability
  gate

Source-proximity — adjacent owner statement ordinals or line ranges in the
upstream blob — is the strongest signal that several small modules were one
original file.

## Locality vs Layer Ownership

Co-consumption is evidence, but architecture ownership is stronger. Do not
co-locate an artifact with its only consumer if that moves domain, policy,
persistence, integration, or infrastructure logic into the wrong layer.

Examples of conventions an architect may infer, not global policy:

- React-like component-local presentation helpers or styling artifacts may
  belong with their sole component consumer.
- Reducers, action constants, and selectors may form a state-management module
  when they share a public contract.
- Parser tables and token predicates may belong with a parser when they are
  internal implementation details.
- Command metadata may belong with a command handler unless it has an
  independent registry or policy role.

Record the scope and exceptions for every inferred convention.

## Convention Induction

Architects should promote understanding through this ladder:

1. Evidence: graph/source facts.
2. Hypothesis: likely convention with scope and counterexamples.
3. Recommendation: concrete worker-ready reorg task.
4. Durable convention: project-local docs updated so future agents and humans
   do not re-litigate it.

Current-state notes should be rewritten in place. "Current-state" means the
best current inference about the decompiled app's internal architecture, not
the current spec's names, paths, or module assignments. Git is the history.

## Directory Shape

The emitted tree is part of the recovered architecture. Architects are
responsible for keeping its taxonomy coherent, not just for judging isolated
module seams.

Directory-shape findings must be grounded in source and graph behavior. A
single-child directory is only a problem when there is no source/graph evidence
for a namespace, route/package, public API, or pending family. A crowded
directory is only a problem when source/graph neighborhoods show multiple
concepts sharing one bucket.

When available, use `reports/tree/<emitted-dir>/index.json` as the first
quantitative view of hierarchy health. Its incoming/outgoing symbol, file, and
edge-kind attribution maps identify which boundary crossings make a directory
leaky. Do not compare directories only by equal depth; evaluate each recursive
directory boundary on its own incoming/outgoing pressure and then read the
source behind the highest-attribution symbols.

Bad directory shapes include:

- too few items: a directory that contains exactly one child, unless it marks a
  stable namespace, route/package boundary, public API boundary, or clearly
  pending family
- too many items: a broad bucket whose sibling list no longer communicates a
  grouping rule
- inconsistent depth: sibling concepts represented with different path depth
  without a project-local reason
- duplicate axes: one concept family spread across competing roots or layers
- mechanical wrappers: `foo/foo.js`-style paths or wrapper directories that
  repeat a name without adding navigational meaning

These are convention problems, not only cleanup chores. When a pattern recurs,
the architect should document the target convention and propose worker-ready
reorg tasks that steer future peels toward it.

## Anti-Patterns

- grab-bag paths like `utils`, `misc`, `core`, or `helpers` without a project
  convention that makes them meaningful
- standalone primitive constants with one consumer
- style/config/data fragments orphaned from the only code that gives them
  meaning
- singleton directory wrappers with no namespace, API, route, or pending-family
  justification
- overloaded directories whose contents span multiple concepts without a
  documented subdivision rule
- parallel homes for the same concept family, such as a domain root, feature
  root, and top-level subject root all owning indistinguishable modules
- preserving chunker accidents as if they were source architecture
- moving lower-layer semantics into a presenter just because the presenter is
  currently the only caller
