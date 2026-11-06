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
Node output. No new test functions or manual test runs were added.

## Product verification

Product-only RBE `30aa7917-7662-4874-b64f-2c42f55385c9` completed SUCCESS at
`93523282acff7a907cd6ea32abc6e267db777e32` in 781.992 seconds. The downloaded
engine matched its advertised digest and length:
`dd53a12d180a28254d9609e8cd9971e4edb1c57466bfd6198f717e6341ffb4c0`,
90,842,008 bytes.

Six ordinary CLI runs with that engine preserve the unannotated reader and
reverse ordering edges, admit the three formerly rejected valid assertions,
retain the already accepted no-array control, and reject the genuine direct
read. Valid overlays narrow two reads and one uninitialized read, while
execution remains incomplete. No application runtime result follows from this.

Initial automated CI caught a fixture input error before the new behavior:
`array` was supplied where the existing enum spells the family `Array`.
Constructing typed `ProgramPremises` and `IntrinsicFamily::Array` removes that
serialization round trip. The initial negative-read fixture passed; the
corrected positive fixture remains subject to automatic PR CI.

The next CI run exposed a separate fixture ownership error: selecting all
bindings left its anonymous callback publication and console observer in the
residual module, creating a genuine side-effect/reader cycle before the
provenance assertion was checked. The fixture now explicitly co-locates those
statements using the existing anonymous-statement helper. Binding selection
alone is not complete source-unit ownership, even in a tiny reproduction.

## Diagnostic lesson

An effect labeled observed means the abstraction recorded a non-fallback
effect; it is not a concrete execution trace. Reduce the source and compare a
real-read control before expanding primitive interpretation or strengthening
author premises. This reproduction does not establish the producer of a similar
edge in a private program; that integration remains a separate check.

The local `bbr` log stopped updating after a hosted runner's `git clean` line
while the separately identified Bazel invocation was active and eventually
succeeded. A stale console tail did not identify the current execution phase.
Use the requested invocation ID for product status/executions and distinguish
it from the hosted-runner ID before reporting a setup stall or restarting work.
