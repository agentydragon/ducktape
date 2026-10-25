# Callback analysis performance regression

## Reproduction

The callback-timing change introduced repeated work at several layers:

- An opaque call invoked the same callback pool recursively, constructing a
  possible-callback argument set for every formal.
- Recursive fallback walked the same owner read closure and promoted its lazy
  reads repeatedly. Limiting depth and memoization entries did not bound that
  aggregate work.
- Capture discovery revisited shared closure snapshots. Structural comparisons
  between separately allocated but equal capture DAGs also repeated their paths.
- Export aliases independently rediscovered the same callback-bearing objects.
- Cached invocation outcomes copied and re-merged the full conservative fallback
  effect set through nested invocations.

The neutral CLI fixtures in `e2e/opaque_callback_scale_test.rs` cover callback
pool width, shared/equal capture DAGs, capture histories across prefixes, exported
aliases in an ESM cycle, dense transitive read closures, growing TDZ dependencies,
and cached invocation fanout. The failing shapes were exercised against the
pre-fix binaries before applying the corresponding changes. Inputs from the
private consumer corpus were not copied into fixtures.

## Implementation and invariants

- Bound actual body expansions per statement independently of cache size. Batch
  reentrant and wide opaque callback pools conservatively.
- Intern capture contexts bottom-up and discover each closure/object once per
  traversal. Discover exports and arguments in batches.
- A fallback retains its callback values as escaped roots. Their static owner
  closure therefore grows monotonically and can be shared across source prefixes.
  Private roots of a directly truncated callable use the shared SCC summary.
- Cache static read/rebind IDs, not their initialization status. Apply them at the
  active prefix and reset statement outcomes/cursors at the next prefix.
- Accumulate fallback effects directly in the top-level statement, preserving
  their original promotion owner. Apply rebinds immediately; precise call effects
  retain their lexical callee owners.
- Merge large sorted effect sets in bulk and apply only new IDs when the escaped
  callback closure grows within a statement.

The work bounds can conservatively add constraints. They do not remove callback
reads or rebinds to achieve the speedup.

## Measurements

Measurements used source-built `-c opt` binaries, pre-staged input, and `perf`.
The controlled private-consumer input contains one large original web chunk,
empty extraction modules, and no vendor swaps. It exercises analysis without
selector solving or the full application spec.

| Input                                       | Earlier measured behavior                  | After the relevant fix               |
| ------------------------------------------- | ------------------------------------------ | ------------------------------------ |
| 1,024-callback pool                         | Merged callback-timing binary exceeds 60 s | Completes; covered by the scale test |
| Separately allocated equal capture DAGs     | Pre-interning binary exceeds 20 s          | Completes; runtime output preserved  |
| 512-prefix callback capture history         | Pre-pool-bound binary exceeds 20 s         | 0.156 s                              |
| Exported aliases sharing one callback graph | Pre-batching binary exceeds 20 s           | 0.351 s                              |
| Cached fallback effect fanout               | Pre-accumulator binary exceeds 20 s        | 2.489 s                              |
| Controlled large web chunk                  | Merged callback-timing binary exceeds 60 s | 25.415 s elapsed, 21.915 s user CPU  |

The last row uses the final production implementation at `c3dd94118c`; the
synthetic timings identify the intermediate change that removed each bottleneck.
The earlier consumer pin completed the controlled input in approximately 4.7 s.
The additional callback-timing analysis still has a runtime cost; this change
restores bounded completion rather than demonstrating parity with that pin.

## Validation

```sh
bbr test -c opt //devinfra/js/debundle/...
```

All 161 tests passed after the bulk-set change. The final incremental-ID change
also passed the scale target and the value-flow, callback-storage, and
intermediate-availability soundness targets. Exact-head PR CI validates the final
implementation independently. The final scale target, including eight cases,
completed in 10.7 s on RBE.

Public invocations:

- Full suite: <https://app.buildbuddy.io/invocation/30c677ed-ca2a-4270-8d92-05d8361554f9>
- Final focused checks: <https://app.buildbuddy.io/invocation/516ba59a-77d2-4cdd-ac65-ae807730d8c4>

The full private application spec is validated separately; success on this
controlled input is not proof that its module assignment passes the gate.
