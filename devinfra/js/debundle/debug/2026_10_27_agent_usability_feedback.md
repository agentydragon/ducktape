# Debundle agent usability feedback

The implementation requests from this round are addressed: identifier guidance
and workflow lessons [#12551], audit progress [#12549], exact-input checkpoints
[#12565], naming-only batches [#12556], filtered raw-edge census [#12555], causal
source witnesses [#12572], and bounded proposal consumer evidence [#12578]. All
passed hosted CI and merged. Standalone optimized-build and adapter-input
preparation guidance also landed in [#12588].

## Remaining adoption work

- Complete full-pipeline validation through a consuming project's canonical
  adapter before repinning it. The isolated naming pilot passed dry-run/apply,
  annotation/selector/template preservation and source-only validation. Its
  manual full audit baseline rejected before probes or checkpoint creation.
  Canonical-adapter and old-pin controls must distinguish input differences,
  spec conflicts and analysis defects before proposing repairs. Source-only
  success does not establish full acceptance or runtime behavior.
- Use the new observed-consumer metadata in a source-reviewed ownership round
  before adding ranking or component/style heuristics. Current readers/modules
  are evidence for review, not inferred semantic owners. Numeric names or small
  files alone do not justify merging code.

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
