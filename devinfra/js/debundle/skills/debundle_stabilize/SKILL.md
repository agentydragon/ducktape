---
name: debundle_stabilize
description: >-
  Author forward-compatible `source_match` selectors for a debundle spec,
  replacing fragile minified-name pins with structural anchors tied to each
  entity's purpose. Not for renaming symbols (debundle_mint_names) or moving
  module boundaries (debundle_architect).
---

# Debundle Stabilize

If debundle tooling blocks the task, follow
`references/docs/bug_reproductions.md` before working around it.

The [task chooser](references/README.md#choose-a-task) links to command recipes.
For selector work, read [authoring portable selectors](references/docs/selectors.md)
for the ladder, anchor strengths, matcher syntax, examples, and selector debt.
Stabilize selectors without renaming symbols or moving module boundaries. Use
that guidance when choosing or repairing `source_matches[]`, `members[].selector` relations,
and `anonymous_statements[].source_match`.

## Judgment and acceptance

`debundle spec validate` proves that a selector resolves to its intended target
on the exact current chunk. It cannot prove that the same selector will survive a
future rebuild. Choose anchors tied to the entity's purpose, then use the
current-source proof as a gate. A unique selector pinned on incidental syntax
is still fragile. The tools resolve one spec against one input program at a time;
they do not compare specs or infer cross-version edits.

`spec synthesize-selectors` is a first-pass converter and a way to compact a
hand-picked anchor. Its exact-AST relaxation fallback can prove an
own-declaration selector when the feature index misses the discriminator,
but the result may retain incidental syntax. Inspect its retained tokens;
`--candidates N` offers alternative anchors. Compare those with grouped
`source_matches[]`, use-site templates, and relational selectors before
committing one. See `selectors.md` § Anchor strength tiers and § Relational
selectors for the choice criteria.

## Worklist

Run both `spec selector-debt` views: the default census for minified name pins,
and the source-aware pass (`--source-file <chunk>`) for near-ambiguous
structural selectors, repeated exact bodies, and grouped-selector suggestions.
Use one module tree and its matching source snapshot for each run. Revisit
`note:` debt when new matcher capabilities land.

When the upstream snapshot changes, identify that exact new input and make a
reviewed draft spec for it; copying an earlier spec is a manual starting point.
Run validation and selector diagnostics against the new source alone. Use
`synthesize-selectors` as a candidate generator, inspect each retained anchor
for the intended entity, and apply only justified edits. Rerun full validation
and the target gate, then report unresolved mismatches.

A selector already using `source_match` may still rest on an incidental anchor.
Read the source and use `match-selector`'s over-pin slack to prioritize review;
zero slack does not establish forward stability.

## Per-member loop

1. Read the binding with `debundle inspect-source --id` and its surrounding
   emitted JS. Identify its role and the stable behavior that names it.
2. Inspect a dry-run `spec synthesize-selectors` candidate for
   `--item <module>:<export>` in JSON. Use `--candidates N` when its anchor is
   incidental. Follow `selectors.md` § The contract and the ladder when
   choosing another shape.
3. Write the chosen selector, grouping bindings that share a declaration when
   useful. Format the YAML after `--apply`, then inspect the diff.
4. Probe an independent template with `spec match-selector`; it checks the
   target and over-pin slack. For selectors with references to other entities,
   use source-only `spec validate` and inspect the reported reference identities.
   Run full `spec validate` through the target's pipeline gate before hand-off.
5. If no purpose-bearing anchor is expressible, keep the name pin and add a
   concrete `note:` describing the missing matcher capability (`selectors.md`
   § Selector debt).

The per-selector commands read the chunk and module tree directly; they need
no dependency graph or pipeline build. Full `spec validate` and `debundle run` need
the Bazel `:debundle` pipeline target with its package roots and repo-root
source root (`cli.md` § Running the pipeline). Use a per-agent Bazel output
base under `/tmp` when other agents are building. On NixOS,
`--server_javabase=…` is a startup option and precedes `build`.

## Boundaries

- Naming minified symbols: `debundle_mint_names`.
- Module boundaries and taxonomy: `debundle_architect`.
- Read-only planning and worklists: `debundle_plan_work`.
