# Analysis compatibility independent of lowering

An importer-name improvement required fresh expensive analysis because the
identity covered the compiled `:pipeline` closure. The new `:staged_analysis`
crate owns projection and artifact schemas, the analysis command, cross-module
summaries, loader derivation, per-chunk fact assembly, context ordinals and
loaded-fact validation. Both analyzer entry points use that implementation.
Lowering imports the same fact computation and reader instead of retaining a
second implementation.

`analysis_identity_source` hashes this complete configured compiled closure
using the existing native-link, toolchain, build-output and data guards.
Inspection of its generated manifest includes `staged_analysis`, `analysis`,
`analysis_codec`, `prepare_chunks`, `program_analysis`, `artifact`, `vendor`,
`spec`, `spec_resolution` and `selector_resolve`; it excludes `pipeline`,
`lowering` and `debundle_cli`. The analyzer executable also excludes those
latter crates. Shared artifact/vendor code remains a conservative boundary.

The `.analysis` format is unchanged. Engine and parser-input checks still
precede body decoding, and exact canonical projection equality precedes use of
the loaded facts. Context numbering retains analyzed runtime ASTs, retained
entry ASTs in chunk order, then reparsed entries. Previous compiled identities
require regeneration, without a fallback. Saved graph and snapshot provenance
continues to require the full running executable digest and its source/spec
checks; analysis compatibility does not grant graph compatibility.

## Build comparisons

The source commit is `c245c6539bd93114123b2df47bc6654d66f50413`, rebased on
`790b6a9b04`. All builds used the Nix devshell, ordinary RBE/cache configuration,
`--config=nolint` and `--remote_download_outputs=toplevel`, requesting
`:analysis_identity_source`, `:debundle_analyze` and `:debundle`.

Temporary source probes changed only:

- A lowering import diagnostic string.
- A decoder-only error context in `analysis_codec::decode`, leaving the writer
  unchanged.
- `TopLevelAwaitFinder::visit_for_of_stmt` to conservatively record an await for
  every for-of loop.

Each probe was restored from exact saved source bytes before the next probe.
The final restored build reproduces all three baseline digests. Prefixes below
are actual SHA-256 values of the generated identity and executable outputs.

| Build                                                                                           | Analysis identity  | Analyzer executable | Full CLI executable |
| ----------------------------------------------------------------------------------------------- | ------------------ | ------------------- | ------------------- |
| [Baseline](https://app.buildbuddy.io/invocation/d29525ab-6b05-4495-86ac-4243b7d365c3)           | `63295435c0ebed76` | `f2ce91fe6613a1e0`  | `1b523b30818d3f38`  |
| [Lowering only](https://app.buildbuddy.io/invocation/ab2af6ed-c70b-469f-90b3-c2043dcbc440)      | `63295435c0ebed76` | `f2ce91fe6613a1e0`  | `dd886f2dde3bd6d1`  |
| [Decoder only](https://app.buildbuddy.io/invocation/847ebb63-a82b-40e3-9c8c-8d696e69458c)       | `64ca0f483e51e51c` | `1614f1de3c1c7a97`  | `749fa894c6cdd6d6`  |
| [Fact analysis only](https://app.buildbuddy.io/invocation/674d8379-0729-4e92-94ae-4f4bb948f311) | `df0faced434e841c` | `5aae0441888c0299`  | `9ff5ea1c5f6a8410`  |
| [Sources restored](https://app.buildbuddy.io/invocation/191005fb-031a-4dab-82d2-42462c47dff2)   | `63295435c0ebed76` | `f2ce91fe6613a1e0`  | `1b523b30818d3f38`  |

These comparisons demonstrate compiled-boundary invalidation, not a measured
canonical runtime improvement. Analyze reuse additionally requires identical
source inputs and canonical projection bytes. Existing staged-artifact and
saved-graph checks remain wired to their current contracts; moved unit cases
have a matching crate target. No local test suite was run for these probes.
The private production runs recorded separately still use their checked-in
engine pin; they are not post-refactor runtime proof.
