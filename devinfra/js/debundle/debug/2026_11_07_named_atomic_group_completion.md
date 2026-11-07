# Explicit completion of a partially claimed atomic group

## Layout problem and bounded change

A partial class extraction in a downstream spec shared a conservative
initialization atom with unclaimed bootstrap code. Narrowing one opaque read
did not free the class. A catchall pulled unrelated residual atoms into the
same owner and introduced an authored transport cycle. The appropriate layout
instruction was to adopt only the remainder of atoms already partly owned by
one named module. Private source and spec audits live in gaffer-private.

`coarse_atomic_groups.complete_partial_atomic_groups_for` provides that explicit
owner list. The pre-subset owner identity must equal the retained post-fold
owner. Mixed authored ownership cannot become eligible through subset filtering;
an omitted sole owner cannot be resurrected. Placement does not alter analysis
inputs or bypass the normal realizability/translation gates. Typed
`atomic_group_owner_completions` reports record the complete and adopted units.

## Neutral CLI reproduction

Input `static/app.js`:

```js
export let first = 0,
  second = 0,
  independent = 7;
first = second + 1;
second = first + 1;
```

Author `first` in `runtime`, `independent` in `independent`, and opt `runtime`
into completion. Leave `second` and both assignments unclaimed. All three
bindings share one source declaration statement, but their source units remain
separate: only the first two and both assignments form the four-unit atom.

Engine commit `621b9c567039440f388c82c773364cfe0708661b` built successfully on RBE
(invocation `2a71ce01-9e92-45b2-92b5-bc71fa01c201`). Normal CLI reproductions,
without new test functions or targets, established:

- Fresh `run --graphs` and project/analyze/saved `run --graphs` accept both gates
  with zero cycles and atomic conflicts; emitted JavaScript is byte-identical.
- The completion report contains one four-unit atom and three adopted units.
  The independent declarator remains separately owned.
- Projected analysis is byte-identical with and without the placement opt-in.
- Without the opt-in, the partial atom rejects.
- An unknown owner path rejects.
- A subset omitting `runtime` accepts using residual placement and reports no
  owner completion.
- Adding a second authored owner rejects. Removing that owner through an
  ownership subset still rejects the partial atom instead of adopting it.

Scratch command receipts, source/spec and digest-verified binary metadata are
under `/tmp/agents/2026_11_07/october-completion/atomic-owner-neutral` and
`atomic-owner-routing`. The binary is 91,267,040 bytes, SHA-256
`97883cd3fd62617032a26d2ab84e7e304d820b12d28916d061a2eff83e7e4f29`.
The downstream normal canonical gate remains an independent requirement; this
neutral proof does not establish runtime equivalence or complete extraction.
