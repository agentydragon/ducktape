# Prepared factory creation receipts

Prepared providers execute in the shared world. They no longer substitute an
imported allocation summary for the provider body. Four independent restrictions
made that execution less precise than its actual operations warranted:

- Closure creation withheld coverage whenever it retained a live cell, or its
  deferred body had opaque syntax, async mode or generator mode. The kernel's
  canonical creation operation only retains cell identities/home objects and
  allocates the closure; it executes neither the body nor parameter defaults.
- Fresh parameter and local declarative cells lost creation/initialization
  receipts merely because a nested closure retained them. Initialized reads of
  the current activation's own bindings also lost coverage in that case.
- An ordinary method's captured home object disqualified its parameter
  prologue, even when the method never read `this`. Prebinding parameters then
  discarded the otherwise complete body ledger.
- The independently checked private callback-factory result premise applied
  only if a legacy summary substituted the call. Prepared execution therefore
  retained the result owner's speculative callback root after a normal private
  factory call, promoting a deferred callback at a later opaque operation.

The operation rules now cover canonical creation and actual fresh declarative
creation/initialization/own-frame reads separately. Parameter prologues retain
their captured environment and run their actual operations. The existing private
factory premise applies to normal prepared execution as well as summary-produced
results. Actual provider effects, live capture cells and allocation events remain
unchanged. No summary replaces prepared execution.

Stores through captured cells, enclosing-activation reads, unsupported argument
prologues, eager callbacks, abrupt operations and unknown callees keep their
independent conservative paths. Creating a closure before a captured `const`
initializes retains its TDZ cell rather than freezing its contents.

## Behavioral evidence

Initial candidate `19402dea-11a8-438b-95e9-40397a3d419f` restored the deferred
opaque callback and inline-factory positives; eager callbacks and source-known
provider writes remained refused. `692df028-e208-48f6-9893-309a3f435cbf` additionally
restored the ordinary namespace-member method and passed local/imported parity,
local call receipts, local declarations, future availability and local-record
factory suites. Its unpublished factory had a complete operation receipt but
still exposed the speculative owner root; that identifies the separate privacy
premise restriction above.

Final `d5eec1ba-a3a8-46b6-9091-b95e7abdf643` passes all eight full suites:
initializer call-effect inputs, purity, unpublished-callable reachability,
local-call receipts, local declarations, future availability, local-record
factories, and local/imported operation parity. The separate focused caller-cell
capture matrix passes in `37bc0375-c835-4a82-ab2b-b251c2ac5e42`. These run the real
CLI, source/emitted Node comparisons, and the applicable publication, eager-call,
captured-store, abrupt-read, source-write and repeated-activation controls.

The full provider-summary suite still has four unrelated failures already
present before this patch: retaining-factory relocation, retained callback after
the swap point, unsummarized provider/constructor classification, and nested
unsubstituted factories. The newly covered caller-capture creation row is a
positive; the independent nested retaining-factory row stays pending.

Complete actual allocation-only bodies can now prove cases previously pinned to
require author operation inputs or a particular foreign-summary inference label.
Those cases verify source/emitted Node output and actual provider call receipts.
Audited input dependency assertions remain; an unsupported try/finally provider
continues to test that an operation annotation cannot invent result execution.

## Remaining fluent-chain limitation

The fluent schema fixture uses member calls on call results, implicit `this`,
and object spread. These operations still lack ordinary ordered transfers. Its
`fluent_exports` classification does not erase prepared-body uncertainty or
replace it with an imported summary. The colocated layout is checked under Node;
the interleaved layout remains conservatively refused. Existing argument-effect
and sibling-export controls continue to bound the annotation's scope.
