# Staged pipeline

The projection, serialized analysis artifact, CLI split, and Bazel actions are
implemented in `staged_analysis/analysis_projection.rs`,
`staged_analysis/analysis_artifact.rs`, `staged_analysis/command.rs`,
`pipeline.rs`, and `pipeline.bzl`. Artifact
identity includes the analysis engine, canonical projection, and parser source
digests. Physical parser-file/spec paths and author notes are report context;
content digests and normalized source paths remain analysis identity.

## Compiled compatibility boundaries

The dedicated analyzer and the standalone CLI share the configured compiled
`:staged_analysis` identity. This closure owns the fact producer, artifact
schema/codec and loaded-fact reader, including source preparation and hygiene
context reconstruction. It excludes `:pipeline`, `:lowering` and `:cli`.
Emission changes confined to those crates can reuse analysis when the sources
and canonical projection remain identical. Shared artifact/vendor dependencies
still invalidate analysis conservatively; splitting those further is separate
work. Saved graphs and snapshots retain their full executable digest.

## Source identity of native premises

The selector solve resolves `resolved_member` native premises to their provider
chunk, original binding and prepared-body declaration index before projection.
Analysis carries that source identity, operation and audited parser-input digest;
logical destinations and public member names are report context. Structural
`source_match` premises retain their selector.

Renaming a logical module or its public member while preserving the selected
source declaration leaves projection and artifact bytes unchanged. Retargeting
the declaration or changing the premise invalidates saved analysis. Reports
restore the current authored selector, note and paths at rendering time, including
when loading an artifact made before a layout edit. Missing, foreign-provider
and non-unique declarations are refused before projection is written.

## Deferred

Per-chunk S3 actions and a separate vendor-strip action remain deferred until a
measured edit mix shows that their additional action and artifact costs pay
back. No measurement in this plan establishes that need.
