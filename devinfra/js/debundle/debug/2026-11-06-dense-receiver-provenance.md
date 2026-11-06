# Dense receiver callback provenance

## Reproduction

The existing initializer `source_only` assertion rejects this neutral shape as
an observed out-of-footprint `later` read:

```js
function readLater() {
  return later;
}
globalThis.saved = readLater;
var table = (() => {
  const values = [];
  values.push((0).toString());
  return values;
})();
var later = "ready";
export { table, readLater };
```

The reproduction uses the existing Array intrinsic-integrity premise and
`chunk_renames.main.annotations.table.initializer_binding_effects: source_only`.
Its assertion is audited under ordinary standard methods and an ordinary global
data store; it supplies the initializer's lexical effect boundary, without a
Number/String integrity promise. Without the assertion, conservative effects
and reader-before-writer ordering are required.

Six ordinary CLI runs at source revision `5ac87c7548` isolated the shape: a
literal-only push still rejects; removing the array admits the assertion;
removing the loop still rejects. A genuine direct `readLater()` invocation
correctly contradicts the assertion. These are small product runs, not
application runtime evidence.

## Candidate repair

The dense receiver capability step in `execute_call_transfer` taints the
receiver and promotes potentially retained callbacks. It previously called
`promote_raw_callbacks` without an assumption, recording possible closure
effects as observed. Use the existing `promote_escaped_callbacks` wrapper at
that one site. It preserves the same callback traversal and conservative
effects, marking this possible closure promotion as assumed.

Actual selected-call invocation remains separate and records genuine source
reads/rebinds. Receiver taint, callback reachability, heap/global effects,
incomplete-execution diagnostics and default ordering remain conservative.
The existing assertion can narrow assumed lexical effects; it cannot override
observed contradictions or certify the whole execution.

The existing staged-analysis fixture functions now include this positive case
under both annotation placements and a direct-read negative control. They retain
checks of raw ordering, saved-analysis identity, materialized effects and emitted
Node output. No new test functions or manual test runs were added. Product
verification of this candidate is still pending.

## Diagnostic lesson

An effect labeled observed means the abstraction recorded a non-fallback
effect; it is not a concrete execution trace. Reduce the source and compare a
real-read control before expanding primitive interpretation or strengthening
author premises. This reproduction does not establish the producer of a similar
edge in a private program; that integration remains a separate check.
