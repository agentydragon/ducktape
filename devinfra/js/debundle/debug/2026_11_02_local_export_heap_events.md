# Local export metadata in the heap timeline

## Selected capability and cause

Three cycle-only downstream chunks have a reciprocal constraint emitted from
their final local export list. The export's synchronous execution is complete
and its purity is pure; the edge's sole source cause is
`array_content_effects`. The other direction comes from an earlier opaque
initializer to a preceding allocation or binding initializer.

`facts/heap_contents.rs::annotate_heap_contents` unconditionally marked every
`StatementKind::Export` as an opaque heap event. This makes metadata such as
`export { local }` depend on the preceding heap frontier even though the list
executes no expression. Grouping that export with residual code can turn the
extra event into a module-order cycle.

The exact downstream source/spec, completed CI and graph identities belong in
[gaffer-private](https://github.com/agentydragon/gaffer-private). This note
contains no private source or spec identifiers.

## Solution choice and proof boundary

- General module evaluation would cover more export forms, with a larger scope.
- A specific syntax proof covers this event: `ExportNamed`, no source module,
  and only named specifiers. It includes local aliases and empty lists.
- An author annotation would ask for a promise the grammar already establishes.

Choose the specific proof. A local export list creates live-binding export
entries; it does not get their values, invoke a getter or call a function at
that source position. Removing the blanket opaque flag leaves the AST
collector's actual effects intact. Other export forms keep the existing
fallback. This does not use purity as proof of heap coverage.

Exported identities remain inputs to the interpreter's export-reentry and
callback-reachability policy. The proof grants no privacy, no-callback premise,
return shape or foreign callable identity. It changes neither assignments nor
gate rules and requires no artifact-format change.

## Expected result and validation boundary

The candidate should remove the export-list heap-frontier edges, including the
reciprocal directions observed in the three downstream witnesses. Remaining
initializer effects and promoted binding reads still constrain placement. A
cycle involving lazy imports can also fail ESM evaluation simulation after its
strict reciprocal edge disappears; acceptance requires the normal canonical
pipeline result rather than an edited-graph projection.

Use existing hosted checks for engine regressions, then inspect normal private
integration artifacts for the selected edges, complete gate inventory and
emitted output. The grammar argument establishes the rule's scope; it does not
establish that the candidate has run or that these chunks now accept.

## Existing CI expectations

The first public [CI run 41440296015](https://github.com/agentydragon/ducktape/actions/runs/41440296015)
at `2f0e9963b` compiled the candidate; 226 of 228 targets passed. Two existing
targets failed: `native_operation_input_test` and
`peel_factorize_landability_test`. The former required a local export list to
have a sequenced edge to a fresh Set. Its runtime-output check and the other
31 cases passed. The latter required an executable decorator closure to absorb
an export declaration, rendering the proposal unaddressable. The reported
proposal contains the class, decorator and surrounding console events, excludes
the export list, and is marked landable. The real tail-to-decorator edge remains.

Update those existing expectations to check the following executable observer's
Set frontier, absence of local-export heap events, the complete landable
decorator closure and its preserved runtime output. Keep the class-only split
rejection and the existing temporary-array ordering assertion. The engine rule
does not change in this repair; normal hosted CI must validate the updated
expectations. Do not cancel a live canonical run to refresh a test-only pin.

The proposal report also retains an export-only advisory row. It is explicitly
unlandable, but can distract from the useful executable closure. A future
presentation improvement can separate metadata-only advisory cells from the
actionable proposals while retaining their explanation.
