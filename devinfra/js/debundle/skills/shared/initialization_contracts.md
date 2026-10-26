# Initialization contracts during cycle recovery

A cycle report identifies ordering constraints, not permission to annotate a
call. Establish the source and runtime premises for the exact contract before
changing the spec.

## Trace the blocker

1. Refresh the graph and read the exact cycle/cut evidence. Record the source unit,
   source location, constraint kind, and called binding.
2. Follow aliases and imports to the defining implementation. Separate eager
   effects, retained callback invocation, and reads before the first `await`.
   Include helpers, provider branches, property accessors, and publication paths.
3. For export-reentry constraints, inspect `export_reentry.json` beside the
   per-chunk dependency graph. Verify the loader and dependency closure before
   selecting a runtime contract.

## Choose the contract for the established fact

The full callback, runtime, metadata, and publication contracts are in the
bundled `references/SPEC.md`. Read the named section before selecting its
assertion.

| Established fact                                                                                               | Contract and audit reference                                                                                                                                                                                                                                                                                    |
| -------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The app uses native browser ESM loading with no synchronous host namespace access during evaluation            | `execution_runtime: native_browser_esm`. The analyzer also checks the static closure, cycles, suspension, and relevant vendor replacements. See SPEC § Runtime loading contract.                                                                                                                                |
| A provider stores a callback in a fresh returned graph without synchronous invocation or publication elsewhere | `callback_factory_exports`, `callback_factory_members`, or the narrower `inline_callback_factory_exports` under `chunk_export_purity.<provider>`. Audit the admitted input/result shapes and all provider paths. See SPEC § Audited callback factories.                                                         |
| A call cannot synchronously invoke foreign callbacks or access importer-owned cells                            | `no_reentry_exports`, `no_reentry_members`, or `no_reentry_record_members` under `chunk_export_purity.<provider>`. Check argument shapes, stable callable identity, and namespace independence where required. Provider-private effects may remain; results stay opaque. See SPEC § Audited callback factories. |
| A decorator helper only mutates inert target metadata and cannot invoke target methods or reenter foreign code | Select the metadata-specific `effect:` matching the helper and argument shape. A target-local mutation assertion alone does not establish callback timing. See SPEC § Local-effect decorator annotations.                                                                                                       |
| A browser-global slot stores a callback that no consumer can use during synchronous initialization             | `chunk_analysis_options.<chunk>.deferred_global_publications`. Audit the host property descriptor and all consumers, as well as the checked source shape. See SPEC § Audited callback factories.                                                                                                                |
| An initializer, call, or construction has no observable effects                                                | Use the purity scope below. Purity does not establish callback deferral.                                                                                                                                                                                                                                        |

These callback, no-reentry, metadata, and global-publication contracts retain
actual eager reads, mutation, and ordering. Source inference also recognizes
supported private callback containers and checked local constructor results;
inspect SPEC § Synchronous analysis limits before
adding an assertion for behavior the analyzer already proves.

## Purity scope

- `initializer_purity: pure` applies to one selected simple top-level variable
  initializer. Audit the entire expression, including synchronous callbacks:
  it must have no observable effects and cannot throw. It does not certify
  later calls or construction of the resulting value. Eager reads and
  callback dependencies remain. Imports, functions, classes, destructuring,
  missing initializers, and ambiguous initialized sites are unsupported.
- `purity: pure` asserts call purity for one local bound function.
- `purity: pure_new` asserts purity only for `new BoundClass(...)`. A selected
  defining class's assertion follows its exports to importers. Prefer a
  structural `source_match` for a minified class.
- `chunk_export_purity.<provider>.pure_exports` and `pure_new_exports` assert
  call or construction purity for named imports from that provider.
- Member-level `pure_members` covers listed static calls through a binding.
  `fluent_exports` asserts the broader transitive derived-value contract;
  audit that complete surface before selecting it.

These are author-trusted assertions. The classifier does not verify the body.
Arguments retain their normal checks; call purity does not certify an export's
initializer or remove independently observed callback dependencies.

## Validate the decision

Record the inspected source, audited provider/runtime premises, and exact
assertion. Rerun source validation, the extraction gate, and the adapter's
runtime smoke. Confirm the intended assignment passes and inspect the retained
constraints. A successful source match alone does not prove runtime behavior.
If the premise cannot be established, retain the conservative constraints and
choose a different module boundary.
