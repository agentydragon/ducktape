# Non-hoisted returned-record callable reduction

This branch contains an intentionally failing real-CLI E2E reduction, not a
candidate acceptance change. On base `8c6a4e8042`, extracting a wrapper and its
later-read value into separate modules passes when the imported provider is a
hoisted function returning `{ invoke: callback }`. Equivalent direct const,
namespace-member alias and initializer-produced alias providers reject with a
two-unit atomic conflict.

RBE invocation `521f078e-190e-4d3b-96f4-233fa7af98fe` ran the four cases:
one positive control passed, three acceptance probes failed. No large callback
pool, author premise or engine modification is involved. The earlier run's
rustfmt-only failure was repaired before these results.

The general foreign-call projection in `cross_module_purity.rs` requires
`call.hoisted`: an initializer may not have executed when a cyclic importer
calls it. Source factory inference is a separate capability. Any extension
must preserve unavailable-export and cyclic-initialization behavior, then
resolve stable alias values where relevant. The failed acyclic control
establishes incompleteness; it does not justify removing that safety condition.

The test source, target and failed gate output are preserved for a future
implementation. Do not merge the failing target without a sound repair and
negative cyclic-initialization coverage.
