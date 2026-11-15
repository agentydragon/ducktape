# Cross-chunk operation parity ledger

An input chunk boundary must not discard facts the same transfer establishes
locally. The comparison uses the same abstract inputs, availability, premises
and resource limits. It does not assume that arbitrary ESM inlining preserves
initialization order. Canonical, runtime and performance checks remain open.
The user approved retaining conservative havoc for difficult unsupported cases;
the precision inventory below is a boundary ledger, not a requirement to model
every JavaScript operation before shipping an improvement.

## Current architecture

Canonical preparation retains one immutable executable source for each module.
The program linker resolves aliases, reexports, namespaces and default cells;
ambiguous, missing and unsupported outcomes stay explicit. Local and imported
calls enter the same operation router and defining-source execution index,
against shared live module cells and heap state. They do not substitute a
`ForeignCallSummary` for an available prepared body. Authored premises retain
separate provenance and do not turn a source fact into an unconditional promise.

Runtime allocation addresses include defining source and activation identity.
Immutable shape queries use the defining source; mutation and alias queries use
the complete runtime address. Invocation replay validates its world/context/read
witness, applies the actual state transition and renames fresh allocations,
lexical cells and captures. Resource exhaustion keeps effects conservatively.
See [runtime identities](2026_11_15_runtime_source_identities.md).

Binding, heap and availability consumers retain qualified program ledgers.
Single-chunk projections remain conservative where they cannot name foreign
cells. Consumers with a complete qualified lane must use it; the
[initializer-contract repair](2026_11_15_initializer_heap_projection.md) records
one missed consumer and its causal tests.

The final emission guard recovers executable owner order from finalized AST
origins and actual static requests, then compares original and candidate ESM
schedules. It checks live-cell observations, completion and opaque loader cuts
independently of purity. [Host setters](2026_11_15_host_setter_initialization.md)
show why an old accepted split can be unsound despite ordinary Node parity.
Diagnostic trees emitted after refusal are divergence evidence only.

## Completion across declaration forms

Schedule validation now lowers initializers from their validated original source
identity. Separate declarations, comma-list fragments and exported declarations
use the same operation kernel; a prepared split view does not lose its original
expression. These plans grant completion only from the actual kernel result and
do not grant independent graph, purity or allocation-only receipts. Bare binding
copies require an initialized local cell or qualified live provider cell.

`saved_analysis_reports_test` covers four separate/comma and plain/exported
forms with real inversions, source/direct/saved Node output `0 3 1`, plus TDZ,
Number/BigInt and BigInt division-error refusals. The focused checkpoint passed
all 13 cases (`62c07ce8-cf3f-4d0e-8f4e-ae033f2a223f`). The previously refused
comma-list lowering fixture also passed with source/emitted Node output
(`0f92fec0-d076-4218-ad31-40a234b150cb`). These checks establish the implemented
grammar, not a completion proof for destructuring, arbitrary coercions or loops.

The Map and descriptor suites keep explicit refusals for nonempty Map
construction, a bare lexical declaration and descriptor inspection of a nested
binding assignment. Their accepted colocated controls retain the original
native graph proofs and source/emitted runtime behavior. Empty `WeakSet`
construction now uses the same checked transfer as the other empty keyed
collections; replacement and iterator/adder reentry controls remain refused.

An opaque-loader boundary also limits ordinary source-analysis receipts. The
linker's closed `source_evaluation_order` refuses a provider importing `path`,
even when host policy declares that builtin nonreentrant. Ordinary analysis then
uses its conservative root-only schedule, so the provider's literal cell stays
uninitialized. The final schedule guard instead retains explicit opaque loader
cuts and executes the prepared provider before its importer. It can accept the
emission while the report-only effect verdict remains incomplete. A same-policy
local calculation keeps its operation receipt; the imported counterpart records
uncovered authority. Skipping opaque requests to manufacture provider readiness
would erase a loader obligation and is not the repair.

Completion-only execution initially bypassed the independent source receipt for
an initialized local binding copy. That lost its noninvoking proof and made an
unrelated opaque construction block an otherwise valid private initialization
crossing. The repair retains the existing source receipt only for the exact
bare local read, with an initialized cell, matching actual prefix values and
current operation dependencies. It does not replay the initializer or derive
noninvocation from normal completion. The minimal real-CLI case failed before
the repair and then preserved source/emitted Node output `0 0`; the focused
conditional suite regained 21 previously passing cases.

Three bounded families remain conservative: indirect eval execution,
unsupported derived construction with a retained writer, and prototype access
with a retained writer. In the latter two, the ordered copy observes
`counter: InitializedValue` in the original schedule and `counter: Unknown` in
the candidate. The final guard keeps this evidence. The conditional suite
records the precise refusal and checks source/rejected diagnostic Node parity
and an accepted colocated control. Matching concrete output does not grant a
missing abstract execution proof. The Function-construction and derived-class
variants that still admit relocation retain their positive runtime controls.
All 63 conditional/logical cases passed in
[`cc1ed865`](https://app.buildbuddy.io/invocation/cc1ed865-a0d6-4311-93b9-a3b9e068784b).

## Transfer inventory and real-CLI evidence

The inventory comes from `local_operations.rs`, not from whichever summaries
happen to be derivable. All its expressions now enter the shared transfer when
prepared provider source is available. Sharing an interpreter does not imply
that every expression earns a complete receipt.

| Local surface                                                                           | CLI evidence                                                                                                  | Remaining qualification                                                                                                          |
| --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `Initialize`, `Evaluate`, `Return`, `Throw`                                             | `cross_chunk_operation_parity_test`, `effect_tv_local_declarations_test`, `synchronous_precision_report_test` | Abrupt/unknown completion must remain ordered; unsupported syntax stays conservative.                                            |
| `Value`, `PrimitiveLiteral`, `Sequence`, `Template`, `Binary`, `Conditional`, `Logical` | Matrix scalar-operands pair; imported primitive receipt/inversion cases                                       | Conditional, logical-and and logical-or have matrix cases; nullish/path-budget combinations remain conservative.                 |
| `Record`, `ReadField`, `AssignField`                                                    | Matrix record-fields pair; independent same-offset provider records; imported array-iteration tests           | Actual getter/prototype/mutation checks remain necessary.                                                                        |
| `Array`, `Hole`, `Value`, `Spread`                                                      | Matrix array-spread pair; imported primitive spread and sparse-factory cases                                  | Some local and imported compositions deliberately retain uncovered authority.                                                    |
| `Assign`, `UpdateBinding`                                                               | Matrix binding-update and reassignment pairs; provider `next`/`peek` divergence                               | Module writes must keep writable ownership; capture stores cannot borrow fresh-local receipts.                                   |
| `Closure`: function, arrow, method, getter, setter                                      | Matrix live closure/returned method cases; retained factory and descriptor suites                             | Fresh invocation cells preserve live captures; ordinary implicit `this`, fluent receivers and object spread remain conservative. |
| `Call`: binding, direct creation, member                                                | Matrix helper/reexport/namespace pairs; initializer-call input suite                                          | Complete body receipts and authored invocation premises remain distinct.                                                         |
| `Construct`, `This`, `DefineThisField`, `AssignThisField`                               | Class-construction suites; imported constructor/callback controls                                             | Checked receiver/prototype conditions apply; some factory calls require phase-preserving co-location.                            |
| `Parameter`, `Var`, `Let`, `Const`, `NamedSelf`, `HoistedFunction`                      | Matrix updates/hoisted-function pairs; declaration suite; future-TDZ local/imported controls                  | Simple bindings only. Fresh invocation cells must not freeze a later capture update.                                             |
| `RootStore::Field`, `RootStore::Binding`                                                | Provider state and record mutation refusals; local array-push order                                           | Known writes stay in their qualified ledger even when a promise covers unknown effects.                                          |
| Native allocation, descriptors, collection/array operations                             | Native-operation, descriptor, imported iteration and repeated factory suites                                  | Native projection must not create an execution identity; every actual allocation has its own event.                              |
| Callback promotion and bounded fallback                                                 | Bounded heap suite; future availability suite; publication-order and namespace controls                       | Reevaluate availability, escapes and graph dependencies; cached source census grants no invocation authority.                    |

The matrix helper compares each local/imported pair's emitted output, module
exports, authority and completion. Its three receipt-positive groups cover
numeric updates, scalar operands and record fields. Its five conservative groups
cover array spread/indexed read, captured closure invocation, hoisted local
function/parameter creation, static method calls and reassignment. A gain in one
of those groups must update the oracle with the same causal execution checks,
not preserve an obsolete refusal merely because it was historical behavior.

Lowering still excludes general loops, `try`, `await`, `yield`, destructuring
parameters, dynamic object keys, object spread and arbitrary member receivers
from the ordered local IR. Existing conservative execution can retain their
source effects; it is not an ordered effect receipt. These local limitations
must be distinguished from an imported-only regression.

The intrinsic iteration fixtures expose another completion boundary. A primitive
sum with `reduce` retains an accepted registry/value split and executable export
checks. Seven row variants involving `some`, `every`, `findIndex`, `find`, an
object-valued reduction or `flatMap` retain their independent graph proofs but
the final schedule guard refuses the unproved reordered execution. Source and
diagnostic rejected output still agree under Node, and the same source is
accepted when colocated. These are conservative precision limits, not evidence
that the concrete splits change semantics. Identical source/spec bytes produce
the same verdict with no diagnostics, graphs only, rejection emission only and
both flags.

Audited constructor factories, asynchronous getters and bounded async callback
fallback also keep independent graph evidence when the final schedule lacks a
completion proof. Accepted factory controls retain separate defining modules
while keeping the creating call and captured later cell in source order. The
at-init and intrinsic suites keep exact refusal reasons and runnable colocated
controls for the unsupported inversions; a graph proof is not discarded merely
because a separate execution obligation remains unproved.

## Placement and invalidation matrix

`cross_chunk_operation_parity_test` also covers shared/separate providers,
alias/reexport/namespace access, identical offsets in different modules,
returned heap aliases, retained provider updates, synchronous/throwing callbacks,
getters, hoisted functions, const TDZ, cyclic var/lexical observation and a
provider throw before later effects. Positive cases run source and emitted Node;
unsafe candidates are refused and their diagnostic output demonstrates the
changed trace. Independent provider cells preserve `1 0 1`; reordering a shared
provider writer/reader changes `1 1` to `1 0` and must be refused.

`saved_analysis_parity_test` covers direct/saved accepted and refused layouts,
provider-source invalidation, reexport inputs, resolved cycles, anonymous default
cells and returned captures. Additional artifact/input/vendor invalidation
suites remain part of the full required check. Artifacts bind source identities
and engine/input closure; execution readiness is never restored from a source
summary alone.

## Checkpoints and unfinished work

The first baseline refused five imported icon/capture placements while their
local controls passed (bceb90f4-27b4-4b84-90e6-bcdad0ef9951). The shared source
integration subsequently passed all 33 matrix cases
(55b26a40-cb04-4725-8584-a467aefa1797). These are focused checkpoints, not full
plan completion. The final guard's own 11 cases and 21 saved parity cases passed
before integration. After rebasing onto `e22b6d443b` and reconciling the phase
oracles, the complete root and e2e aggregate passed all 300 test targets at
`8755905be6` (plus this ledger's precision notes), invocation
[`026bbda2`](https://app.buildbuddy.io/invocation/026bbda2-952c-4073-8cce-e82dbe98859f).
All 402 requested targets built. Three fixture targets executed and 297 reused
passing action-cache results. This covers the implemented transfer subset,
saved/direct cases, existing positive controls and adversarial refusals. It does
not include the canonical Excalidraw or private maintained-spec targets.

The [foreign fallback availability repair](2026_11_15_foreign_callback_availability.md)
closes a real local/imported gap: stop at a future lexical initializer's TDZ,
retain its binding dependency and revisit after initialization. Callback graph
memoization has [explicit read dependencies](2026_11_15_foreign_callback_reach_memo.md)
but has not solved the canonical or registration cost.

The [maintained-spec resource probe](2026_11_15_maintained_spec_resource_limits.md)
also remains unsuccessful: Claude CLI analysis repeatedly hit executor OOM
limits, and Tana analysis was killed without a successful result. The target
summary's Tana `BUILT` label conflicts with its aborted BEP event and is not
acceptance evidence.

The supported subset and the conservative phase corrections above are now
covered by the passing aggregate. The engine change is
[PR #16762](https://github.com/agentydragon/ducktape/pull/16762); the upstream
icons candidate and its output/browser assertions are separate dependent
[PR #16763](https://github.com/agentydragon/ducktape/pull/16763).

Remaining acceptance work is explicit: complete the real pinned icons module
and unchanged canonical control under the normal gate, verify Node/browser
behavior and maintained specs, and obtain matched completed CPU/wall/RSS
measurements. Large-input resource failures remain open. A capped profile or a
passing minimized icon fixture is not canonical extraction acceptance. These
limits keep both PRs in draft; the root/e2e pass does not establish full plan or
maintained-product acceptance.
