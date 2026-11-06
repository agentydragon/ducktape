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

Analysis-only retention uses `JsFileBody::SourceWithAst`: it borrows the original
`SourceFile` without copying its bytes. Read-only source queries and emission
preserve the previous source representation, including minified spacing,
comments, and header behavior. Mutable AST access promotes the body to ordinary
`Ast` before exposing it; consuming AST parts likewise drops the source snapshot.
Source-marker labels therefore keep their existing source/staged-body contract.
Cloning retains the same original source while allowing independent mutation.
Both `EmissionFiles` constructors release analysis-only syntax back to source
storage before eager emission mutation passes. This preserves which pass-through
files participate in rewrites and their exact source bytes; indexes are reused.

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
provider, source-marker/minified/comment pass-through parity, outside-closure refusal, source-match/member site parity, stale source
provenance, and direct versus staged saved-analysis parity.

The neutral marker/minified/comment CLI control fails on the initial retention
head `1f43a3954d` (with the test-only patch) in BuildBuddy invocation
`76c505b1-4fd9-4270-9d0c-5cbacd43cb09`: adding a source-only premise changes
source-marker matching from one provider to zero. The repaired representation
must preserve both provider selection and actual written provider bytes.
