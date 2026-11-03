# Commuting pairs must preserve third-statement order

## Reproduced failure

The normal CLI accepted a split whose emitted Node output differed from the
original: `1 undefined undefined 1` instead of `0 undefined undefined 1`.
The exact commuting assertion was true: reading an array's length commutes
with an optional call of a known empty function.

The synthetic source allocates an array, reads its length, calls the no-op,
then pushes one element. The read is in an observer module; the no-op and
write are in a mutator module. An earlier harmless constant puts that mutator
first in the entry's otherwise-unconstrained module order. The fixture uses
the existing dataflow-aware and trusted-summary options.

Before the candidate engine change, the CLI/Node regression failed in
[7121f38a](https://app.buildbuddy.io/invocation/7121f38a-7365-4af3-ad5f-c29c7cf37896).
The source output was checked first; the mismatch was emitted output.

## Cause and required invariant

The heap frontier retained `writer -> noop -> reader` and cleared the earlier
reader from its cell timeline. Post-build application of `noop`/`reader`
commutation only marked their direct edge inert. The writer then had no path
to the reader. The same problem applies to other compacted ordering chains.

A pair assertion licenses swapping only that pair. Every original ordering
path between a different pair must survive. Preserve the two-edge paths
around inert edges, and repeat for added paths so overlapping assertions cannot
jointly drop a third pair's ordering. Keep typed provenance for those edges.
Check unused assertions after this closure, since one assertion can expose the
edge another assertion certifies. Eager-use and other non-sequence edges stay
active, and definite cell conflicts still reject assertions.

This is a shared graph correction, not a new effect model or stronger author
promise. Full downstream acceptance and runtime proof remain separate checks.

## Validation and iteration

- The corrected graph passes the real CLI/Node regression in both directions:
  commuting the read with the no-op, and commuting the write with the no-op.
  Overlapping assertions pass in both spec orders, including an assertion
  whose edge is exposed only by another assertion.
- The four commutation resolution/gate/proposal/budget suites passed in
  [1a03574e](https://app.buildbuddy.io/invocation/1a03574e-c79a-4ed6-99d8-19c7a6d3953a).
  The final additional runtime direction passed with the gate suite in
  [62f6a16e](https://app.buildbuddy.io/invocation/62f6a16e-df63-41ed-9389-88210551c8fc).
- The existing repeated-`var` test caught an overrestriction in the first
  candidate. Co-declaration, rebind and local-effect edges express co-location,
  not execution order inside that destination. The closure now composes only
  temporal `eager_use` and `sequenced` edges; the original test passes.
- An RST_STREAM ended the test log connection although the remote command
  later succeeded. Reconnecting the same outer invocation with `bb view`
  recovered the completed result; a stream failure alone is not a test result.

For downstream graph changes, recover the matching analysis through the
pipeline's `analysis` output group and compare the unchanged and corrected
graph builders against it. Keep other fact changes out of that comparison;
the normal CLI is the `:debundle` binary, distinct from `:debundle_analyze`.
