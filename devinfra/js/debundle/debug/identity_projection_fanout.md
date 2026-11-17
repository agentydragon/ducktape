# Shared identity returns multiply property projections

The reduced source is:

```javascript
function identity(value) {
  return value;
}
const a = identity({ value: {} }).value;
const b = identity({ value: {} }).value;
console.log(a === b); // false
```

Scale it by generating more independent declarations of the same shape. Each
declaration must contain a distinct object literal in source; a JavaScript loop
would reuse an allocation site and would not reproduce this relation.

The spec needs one chunk, no extracted modules and `inline_in_entry` ownership.
The [benchmark](../e2e/bench/polymorphic_identity_bench_test.rs) generates the
bundle/spec pairs at 128, 512 and 2,048 calls, runs the real CLI and verifies
both original and emitted JavaScript print `false`. It also runs the equivalent
direct-object control and publishes each bundle and OTLP JSONL file as Bazel
undeclared outputs:

```shell
bbr test -c opt //devinfra/js/debundle/e2e/bench:polymorphic_identity_bench_test
```

This is an opt-in measurement benchmark, not a regression test claiming an
optimization. It intentionally does not assert that the excessive work persists.

## Measured work

Optimized, unmodified devel `a522ad2320d1ef91ba995b83683bb4b6ed6633c7`, including
the property-producer census:
[tool build](https://app.buildbuddy.io/invocation/c1c99f59-0006-4522-b19f-75d240d202d9).
Native analyzer measurements use prebuilt projections, four Rayon threads,
enabled work counters and GNU time. These are individual observations, not
medians; the operation counts establish the scaling independently of timings.

| Calls |     Reads | New subscriptions | Analyzer wall |
| ----: | --------: | ----------------: | ------------: |
|   128 |    17,156 |            16,899 |        0.04 s |
|   512 |   265,220 |           264,195 |        0.27 s |
| 2,048 | 4,206,596 |         4,202,499 |       10.47 s |

In this family the observed totals are `N² + 6N + 4` reads and
`N² + 4N + 3` subscriptions. At 2,048 calls, replacing the identity calls with
direct objects reduces the totals to 4,099 reads and 2,051 subscriptions, with
0.15 s analyzer wall. Keeping the identity calls but removing the field
projections gives 12,292 reads, 8,195 subscriptions and 0.34 s. Analyzer peak
RSS was respectively 190.2 MiB, 60.6 MiB and 65.6 MiB.

A separate `perf record` run of the unmodified 2,048-call analyzer captured
596 samples at 49 Hz. Bitmap-store subset checks accounted for 47.80% of
sampled cycles and array-store unions for 33.16%. This locates the CPU cost
beyond the visible dependency count; it is not a timing comparison with the
unprofiled runs.

The real CLI benchmark passed on
[RBE](https://app.buildbuddy.io/invocation/e5b8a6a5-f8b9-41bf-882c-2277eea73ed2),
including emitted Node execution for all six bundle/spec pairs.

## Mechanism

`FlowRules::invoke` merges all actual arguments into one formal binding cell.
The function's `Return` event copies that union into one return cell. Every
`identity(...)` expression reads that shared return cell: call arguments are
separate events, not part of `FlowValueExpr::Call`'s result identity.

Consequently all `N` initializers have the same abstract expression:
`Member(Call(identity), "value")`. Each receives the same `N` outer-object
origins. `append_value_expr` nevertheless looks up the field on every possible
receiver separately for every initializer, building `N²` property dependencies
and repeatedly rebuilding the same union of the `N` child-object origins.
`ValuesBuilder::extend` uses `union_bitmap_observed`, which checks subset
relations and performs pairwise unions as that result grows.

Each variable event executes only twice in the measured family: initially and
after the shared return grows. The excessive work is inside those evaluations;
reducing redundant scheduling alone would not share their identical projection.

The producer census cannot omit these watches: every field has a real producer.
Using primitive field values preserves the quadratic subscriptions but removes
the large result bitmaps. Removing the fields entirely lets the census omit
their subscriptions, but still produces `N²` missing-property decisions while
enumerating receivers. Neither variant reproduces both costs as directly as the
object-valued field above.

This is a conservative loss of call precision combined with repeated evaluation
of an identical projection. It is not an incorrect emitted-JavaScript result.
Sharing the projection computation would address redundant work without first
requiring a more precise call abstraction.

## Connection to Tana

Tana's shared default-export wrapper returns its argument or that argument's
`default` member. The reduced fixture retains the argument-return operation
responsible for pooling and removes the default-property branch. In snapshot
`78d928dca7`, the wrapper starts at byte offset 2,783, line 81 of the captured
vendor chunk, SHA256
`9809cb4d96458cfb235e23fc8d4f6eae79e76efcd1b1d118b8b95ff482c17f3b`.

Temporary count-only instrumentation on the same devel revision records its
shared return cell 56556 being read by multiple independent variable
initializers, with 27 possible origins and later 48. These include unrelated
object and function origins. The
[transfer witnesses](../perf/analysis_serial_phases.md#first-origin-breadth-transfers-2026-11-17)
independently establish the consequence: the pooled argument-or-default return
introduces unrelated callees, including deep merge, into a variable used as a
throttle helper. That is observed cross-call pollution in Tana, not an
inference from the synthetic timing.

A separate vendor-local identity callback's formal and return reach 20,264
possible origins. The follow-up probe counts 15,043 object/set origins and
5,210 function origins in that return; no consumer read of this particular
return was observed before the cap. It demonstrates breadth, not an attributed
hot projection. Origin widths include any unknown alternative and must not be
described as distinct concrete objects.

Temporary analyzer builds:
[first witness](https://app.buildbuddy.io/invocation/8354c01a-d472-4347-9ba8-256605a77e5f),
[return-read witness](https://app.buildbuddy.io/invocation/ea0eb56e-e3ec-4fbf-a825-df6a6333e882).
Instrumentation observes existing reads and proposals without adding semantic
operations and is excluded from production. Each Tana probe has a 120-second,
16-GiB/no-swap cap and remains inside source value flow; both report 9.5 GiB
peak unit memory. The synthetic measurement does not establish what fraction
of Tana's total cost this mechanism causes.

Native inputs, tool identities, metrics, GNU time outputs, the bounded private
witnesses and `perf` outputs are under
`/tmp/agents/2026_11_17/tana-minimal-flow/`. Those local receipts can expire;
private source and witnesses are not part of the public fixture.
