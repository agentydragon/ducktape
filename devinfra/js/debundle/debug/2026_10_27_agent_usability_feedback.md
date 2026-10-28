# Debundle agent usability feedback

The implementation requests from this round are addressed: identifier guidance
and workflow lessons [#12551], audit progress [#12549], exact-input checkpoints
[#12565], naming-only batches [#12556], filtered raw-edge census [#12555], causal
source witnesses [#12572], and bounded proposal consumer evidence [#12578]. All
passed hosted CI and merged. Standalone optimized-build and adapter-input
preparation guidance also landed in [#12588].
Audit probes now reuse immutable input preparation while rerunning the
contract-dependent analysis and validation [#12600]. The measured improvement
and reproducible optimized-build recipe live in the
[contract-audit performance note](../perf/contract_audit.md).

## Remaining adoption work

- Complete full-pipeline validation through a consuming project's canonical
  adapter before repinning it. The isolated naming pilot passed dry-run/apply,
  annotation/selector/template preservation and source-only validation. Its
  full audit baseline rejected before probes or checkpoint creation. Those
  initial controls used an older consuming-project checkout and are historical
  evidence. On the current committed source/spec pair, a fresh canonical
  adapter execution passes with the working pin; a newer engine rejects five
  atomic groups. Preserve the working pin while tracing the changed ordering
  facts to their source. An impurity diagnostic alone does not prove impurity
  changed between engines. Candidate acceptance, regenerated-output equality,
  and runtime behavior still need validation; source-only success does not
  establish them.
- Use the new observed-consumer metadata in a source-reviewed ownership round
  before adding ranking or component/style heuristics. Current readers/modules
  are evidence for review, not inferred semantic owners. Numeric names or small
  files alone do not justify merging code.

## Newly observed diagnostic gap

Expose native-integrity proof loss with its mutation witness and affected
guard. The current adoption investigation traced unknown global-slot writes to
`UnknownGlobalValue`, then `GlobalObject`, which withdraws array-intrinsic and
ordinary-object-prototype guarantees. The resulting opaque-call fallback can
add eager reads and atomic conflicts. Current reports expose unsupported
construction/call targets and callback-pool limits, but connecting those losses
to an unrelated global-property write required implementation inspection.

A neutral CLI control building a registry with
`Object.fromEntries(entries.map(...))` and a stored deferred callback passes on
both engines. Adding `function publish() { globalThis.slot.flag = true; }`
passes on `ecb4a734` and rejects on `2303f0d6`; assigning `globalThis.slot = {}`
first does not restore acceptance. This is stricter unsupported reconstruction,
not evidence that the new guard is unsound or that a purity contract is needed.
An unknown slot can alias a native object. Recovering precision requires an
identity proof with accessor, replacement and reentry guards; ignoring a property
name would not supply it. Preserve this distinction in the diagnostic.

The new commands retain conservative boundaries: independent audit removals do
not establish joint removability; names-only edits still need source validation;
filtered edge inspection is separate from the gate; unsupported or ambiguous
causal links remain inconclusive. Durable contracts live in the
[CLI reference](../docs/cli.md), [naming workflow](../docs/naming.md), and
[proposer guide](../docs/peel_proposer.md).

For large standalone runs, follow the
[optimized CLI and adapter-input recipe](../docs/bazel_integration.md#standalone-cli-on-captured-bundles).
The Rust executable follows the requested compilation mode even though its
solver subtree is optimized. Reuse the adapter's actual input roots, generated
vendor/source targets and package mappings.

Examples and results here are generic. Private source, identifiers and
diagnostic payloads remain in the consuming repository's local artifacts.
Build/download, billing and checkout problems belong with their infrastructure
owners rather than in this Debundle feature list.

[#12551]: https://github.com/agentydragon/ducktape/pull/12551
[#12549]: https://github.com/agentydragon/ducktape/pull/12549
[#12565]: https://github.com/agentydragon/ducktape/pull/12565
[#12556]: https://github.com/agentydragon/ducktape/pull/12556
[#12555]: https://github.com/agentydragon/ducktape/pull/12555
[#12572]: https://github.com/agentydragon/ducktape/pull/12572
[#12578]: https://github.com/agentydragon/ducktape/pull/12578
[#12588]: https://github.com/agentydragon/ducktape/pull/12588
[#12600]: https://github.com/agentydragon/ducktape/pull/12600
