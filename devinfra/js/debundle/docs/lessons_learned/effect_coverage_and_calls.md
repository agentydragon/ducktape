# Owner effect coverage, call admission and commutation

Lessons from extending original-owner initializer coverage, ordered call
admission and commuting-statement premises (2026-11-02 to 2026-11-07). The
contracts are in <../../SPEC.md> and <../design.md>; this records what broke or
misled along the way.

## Ordering

- **A commuting pair licenses only that pair.** Marking the commuting edge
  inert after graph construction dropped the order of a third pair: the heap
  frontier had kept `writer -> noop -> reader`, so the writer lost its only
  path to the reader and Node printed `1 undefined undefined 1` instead of
  `0 undefined undefined 1`. Every two-edge path around an inert edge must
  survive, repeated for overlapping assertions, and unused-assertion checks run
  after that closure (one assertion can expose the edge another certifies).
  Only temporal `eager_use` and `sequenced` edges compose: co-declaration,
  rebind and local-effect edges express co-location, and composing them
  over-restricted repeated `var` declarations.
- **The proposer must use the same preservation.** `propose-commutations`
  predicted `pairs_break` for the same shape while the gate kept the cycle.
  Graph construction and the proposer share one temporal-relation closure; a
  combined proposal can stop at the first surviving cycle and mark its counts
  as lower bounds. A correct cut prediction does not prove the assertion true.
- **An export list executes nothing.** `export { local }` creates live-binding
  entries without reading values, yet was marked an opaque heap event, which
  could turn grouping it with residual code into a module-order cycle. Only
  local named export lists get this proof; default expressions and classes
  execute code.

## Calls

- **A global-object lookup names no callback.** An unknown member of the global
  object used to return `Top` plus every escaped callback as an exact target,
  so their effects were recorded `observed` and blocked a valid commutation
  (`commuting_statements_gate_test`). It returns `Top` only; possible-call
  dispatch still expands the escaped pool under opaque-callback attribution.
- **Receivers are captured with the reference.** In
  `receiver.select((receiver = replacement))` the method and its `this` come
  from the original object. An arrow returned by a method can retain `this`
  without an identifier capture, so local method results need the
  unused-receiver check including nested implicit captures.
- **Lookup and result transport migrate together.** Moving member calls onto
  the ordered path first lost the source-summary `push` proof inside an
  argument helper and created a rejected atomic group. The guard serves both
  callee lookup and invocation.
- **Refusals must say which guard failed.** A generic `opaque_execution` or
  `factory_identity` reason sent investigations toward the callee or a new
  premise when the cause was a missing result transfer, a declined target, or
  publication of the factory. Keep target refusal, missing result and
  incomplete invocation distinct; an unchanged `complete` flag on a declined
  transfer is not a timing proof. A hint for an opaque initializer must not
  suggest `purity: pure` or another contract without its prerequisites.
- **Terminal calls have no remainder.** The ordered caller's second fallback
  published its whole frame, including the called factory, even when the call
  was the body's final return; the later direct call then lost its factory
  proof.

## Owner coverage

- Creating a value is not running it: function creation evaluates neither
  parameters nor body (defaults, destructuring and rest are lazy), a class
  declaration evaluates no instance field, and a regex literal goes through
  `RegExpCreate` without looking up the mutable global `RegExp`. Each
  certificate covers creation only; the later call or construction owner stays
  uncovered.
- Comma declarations are lowered to one singleton view per declarator; the
  certificate needs the original parent and declarator index, because a cloned
  singleton has lost that lineage.
- An exact author receipt (`initializer_call_effect_inputs`) closes the
  operation domain only. `dependency_graph.json.gz` can still show an opaque
  legacy heap frontier and an uncovered binding-effect receipt for the same
  owner; read `effect_translation_validation.json` for the receipt.
- A coverage count or an empty observed-access ledger never licenses removing a
  real observation constraint, such as an opaque array push or a property read
  after an opaque operation.

## Fixture traps

- Module path order alone does not establish an emitted inversion. Keep one
  side residual in the entry and check the generated import order.
- The conflict stream lists conflicting reordered pairs, not every retained
  access. To assert a binding conflict, make the read observable; a discarded
  comma operand is a poor control, and replacing a completing `var` read with a
  throwing TDZ read changes the premise.
- When ordered execution removes an expected diagnostic, look for an earlier
  abrupt path in the fixture (a TDZ throw stops the body) before restoring the
  old loss or weakening the assertion; flattening had hidden an unreachable
  assertion.
- `full_semantic_precision: false` in the effect report is a fixed policy
  field; read per-chunk coverage and refusal counts instead.
- Documents shipped in skill archives must pass the archive's relative-link
  check: a repository-valid link from the shared SPEC to `docs/` is broken in
  an installed skill.
- An exact source-unit inspection of a comma-split declarator shows the whole
  original statement; its siblings are context, not selected owners.
