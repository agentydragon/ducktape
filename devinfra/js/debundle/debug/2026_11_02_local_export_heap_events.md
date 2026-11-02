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
