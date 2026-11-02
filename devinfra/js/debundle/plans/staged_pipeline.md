# Staged pipeline

The projection, serialized analysis artifact, CLI split, and Bazel actions are
implemented in `lowering/analysis_projection.rs`,
`lowering/analysis_artifact.rs`, `pipeline.rs`, and `pipeline.bzl`. Artifact
identity includes the analysis engine, canonical projection, and parser source
digests. Physical parser-file/spec paths and author notes are report context;
content digests and normalized source paths remain analysis identity.

## Remaining selector-identity decision

`resolved_member` premises still carry their logical destination into analysis:
`AnalysisProjection::new` keys `premise_members` by `(module, name)`, the
projection retains the authored selector, and
`resolve_native_operation_inputs` retains `ResolvedMember` selector provenance.
A module rename therefore changes analysis identity when such a premise is
present, even if selector resolution reaches the same source declaration.

Decide whether the logical module path is intentionally part of a premise's
identity or whether analysis should key it by the resolved provider declaration
and leave the authored path in report-only evidence. The current
`assignment_edit_keeps_the_projection_and_the_artifact` case does not exercise a
`resolved_member` premise, so this boundary is not proven either way. No
canonical gate payoff has been established for changing it.

If the selector is made destination-independent, add a neutral fixture with a
`resolved_member` premise that renames its module while keeping the same source
declaration; require identical projection and artifact bytes. Changing the
selected declaration or premise must still invalidate the artifact.

## Deferred

Per-chunk S3 actions and a separate vendor-strip action remain deferred until a
measured edit mix shows that their additional action and artifact costs pay
back. No measurement in this plan establishes that need.
