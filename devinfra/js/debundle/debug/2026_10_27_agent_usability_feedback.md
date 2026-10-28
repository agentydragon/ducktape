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

## Native-integrity diagnostics and remaining precision

Native-integrity reports [#12624] now connect evaluated ordinary-object and array
guard failures to source write-census witnesses. The graph retains each witness
once, including its chunk, statement/location, operation, receiver alias roots,
affected families and invalidated key premises. Bounded `describe` follows those
references across chunks. These are conservative source facts, including deferred
writes; they do not prove a write executes or uniquely causes a loss. An
exact-input downstream replay retained its rejection report and exposed bounded
mutation witnesses; it does not establish upgrade acceptance.

Ordinary receiver identity remains an adoption blocker. Unknown global-slot
values can alias native objects; assigning a fresh RHS does not prove the slot
retains it, because a setter can discard it. A public operation can also receive
a different object through a borrowed method. The runtime-backed negative
controls and identity-certificate plan in [#12625] preserve these boundaries;
they do not restore acceptance of ordinary namespace-slot workloads or establish
a successful consuming-project upgrade. Precision recovery still needs accessor,
replacement, alias and reentry proofs. Preserve the working pin until the
canonical adapter, regenerated outputs and runtime behavior validate together.

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
[#12624]: https://github.com/agentydragon/ducktape/pull/12624
[#12625]: https://github.com/agentydragon/ducktape/pull/12625
