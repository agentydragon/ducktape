# Receiver confinement prerequisite

The PF5 fixture in `e2e/callbacks/iteration_receiver.rs` remains a safe
refusal. Its desired split moves `later` into a module. The original sequence
is:

```javascript
import { host } from "./peer.js";
const entries = [() => later];
entries.forEach(() => host(1));
const first = entries[0];
const later = 42;
console.log(first());
```

The peer prints its argument. The unchanged entry prints `1` then `42`;
removing the opaque call permits the split. Passing the callback's third
parameter to `host` instead must retain refusal: the peer can invoke
`array[0]()` before `later` initializes and observe `ReferenceError`.

## Existing facts and authority boundaries

- `CallableBodyFacts::ordered_plan()` supplies a `LocalBody` only when lowering
  covers every live source operation. That is syntax coverage, not a complete
  may-effect or confinement result after an unsupported operation.
- `native/array_iteration.rs::iterate_array` consumes the selected native row
  and receiver snapshot. It requires complete callback execution, no unknown
  call or precision loss, and an exact normal return. An opaque callback takes
  `ArrayIterationCallbackIncomplete` through `refuse_iteration`, which includes
  the receiver in fallback reach.
- `adapters/application.rs::call` preserves partial effects, then summarizes
  the remainder. Its terminal-call exception covers a selected ordinary local
  target; it supplies neither a foreign-call confinement proof nor normal
  completion. `extend_with_remainder` collects later indexed references and
  callable sites, without proving their effects disjoint from a receiver.
- `interpreter/evaluation.rs::local_operation_fallback` publishes `frame.inputs`, the
  environment, active cells and fresh records. Thus an implicit iteration
  input can reach the stored callback without having been an opaque operand.
  `bounded_invocation_fallback_transfer` unsettles those values and promotes
  their callable effects.
- `roots/body.rs` separately calls `unsettle_incomplete_reach` for an incomplete
  top-level owner. Preserving a slot only inside the native row would not
  survive this finalization boundary.
- `graph/build/at_init.rs::promote_at_init_calls` retains unresolved source-read closure
  promotion for incomplete owners. `HavocReach` is diagnostic, not authority
  for dropping those edges. The array exemption in `facts/at_init_fallback.rs`
  is restricted to call-free callbacks and cannot discharge this fixture.

The existing stored-reference visitor is reusable carrier inventory, but
`reachable_objects` is not a sealed complete may-reach proof: it traverses
stored references, while callable source reads, unknown values, foreign
surfaces and escaped/exported roots have additional consumers. Absence from
that walk cannot establish confinement. `RuntimeKey` currently indexes
ordered prefix/request/result/property/memo facts; it does not provide the
receiver-specific complete-remainder relation or its dependency index.

## Minimum next implementation

Extend the shared `LocalBody` operation/effect machinery with a bounded
receiver-preservation projection that can finish while normal execution stays
incomplete. It must cover the entire admitted body and remainder, the explicit
opaque operands, and escaped/exported/reentry reach. Unsupported operations,
captures or hidden reach refuse the projection. Reuse the current source
identities and operation definitions; do not add another AST recognizer.

Carry the resulting receiver/cut/slot capability and all consumed dependencies
through native-row refusal, local fallback, top-level settlement and graph
fallback. Each consumer needs the same proof, including invalidation on a
later write or publication. Merely filtering implicit arguments, accepting a
`LocalBody` as effect completeness, or changing `exact_normal` would leave one
of these authority boundaries unproved. The finite tuple, propagation,
invalidation and lifetime bounds remain the acceptance criteria in the active
consolidation plan; this investigation establishes no implementation or
performance result.

## Reproduction

`bbr test //devinfra/js/debundle/e2e:callbacks_test` runs the fixture's
original/emitted Node checks for the primitive and escaping variants, the
accepted extraction with the opaque call removed, and both expected split
refusals. It passing confirms the current refusal, not receiver-preserving
extraction.
