# Source-premise provider AST retention

The frontend source-site resolver still reparsed source-only providers after
`prepare_js_chunks` had parsed them and released their AST. That path used the
rendered source representation instead of the original retained parser identity.

Source preparation now takes an explicit analysis-provider retention set in
addition to materialization selection. Live preparation derives it from the five
configured source-premise families. Projection-based Analyze derives it from
canonical source-site provider provenance keys, so an imported provider outside
`analysed_chunks` receives the same retained original AST in both processes.
Frontend resolution requires that AST and has no fallback parse. Static-import
census supplies no analysis-provider retention request.

The source entry/path/selection helpers and source-marker policy-label resolver
now belong to `artifact`; frontend has no dependency on the staged analysis
execution target. Source-marker resolution only maps configured labels to loaded
provider IDs and adds no semantic inference.

## Remaining structural reuse limit

This correction does not migrate the structural-fact schema. The frontend's
joint ownership solve consumes its `selector_resolve::Chunk`, then returns its
owned `StructuralChunkSourceFacts` for later analysis. Provider source-premise
resolution still builds a selector chunk and structural census from the retained
AST, using the existing collector and resolver. Reusing the selected chunk's
consumed selector/index would require changing that ownership API. Likewise,
legacy whole-program analysis may reparse other entries released by source
preparation. Neither path is a second frontend provider parser after this fix;
unifying all source facts/selector indexes is a separate architecture slice.

## Validation

The preparation storage-invariant control loads real provider and neighbor files,
then checks that only the separately requested provider keeps its AST without
materialization. Real CLI controls cover an unmaterialized captured-value
provider, outside-closure refusal, source-match/member site parity, stale source
provenance, and direct versus staged saved-analysis parity.
