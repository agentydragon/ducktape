# Kernel design: what remains

Wave 2 of <convergence_landing.md> landed as L2a–L2d. The contracts that
landed (the five context traits, observations returned rather than recorded,
one `World` for both adapters, the `facts/sync_effects/` layout) are in
<../docs/design.md> § The synchronous execution kernel. This file holds the
kernel work that has no caller or no construct on devel yet. Each entry
leaves when its slice lands. `Outcome.complete` stays until the authority
flip: see <convergence_landing.md> § Wave 4, L4d.

Evidence references (`path:line`) point into `facts/sync_effects/` at the
#14552 head `839549c0f4` (tag `debundle-reference-14552`), the reference
implementation being ported.

## Forks and joins: what is left

Conditional and logical operators fork the body and `alternatives/` joins
the family when every leaf has completed; the contract is in
<../docs/design.md> § The synchronous execution kernel. Left:

- **Path-relative call receipts.** `EffectCoverageFragment::join_alternatives`
  keeps the lexical receipt only when every leaf selected the same call
  edges, so a body with a call on one arm only has no receipt. The reference
  head carries branch-only calls in a may-obligation ledger
  (`effect_coverage.rs:1913`, `call_observations`); the root seal has to
  accept a may-edge before that can land.
- **Root admission.** A root initializer whose only reason for the ordered
  lane is a conditional or logical operator still takes the flat lane
  (`roots/operands.rs`, the `ordered_initializer` families). Admitting it
  needs a typed loss for an unproved member read at the root instead of the
  remainder summary, or legacy-accepted roots regress.

  **Admission is only safe when the kernel can finish the root.** A refusal
  after some callbacks have run cannot be handed back to the legacy row (that
  would be a second evaluator or a retry), so it becomes a remainder summary
  with global reach, strictly worse than what the flat lane produced for the
  same root. Admitting every root whose one invocation is an iteration row
  regressed three e2e targets: `intrinsic_array_iteration_test`
  (`entries.reduce((t, e) => t + e.size, 0)` over records holding
  `executor` closures: a Node-verified legacy acceptance became an atomic
  group rejection), `at_init_unresolved_callee_test`
  (`tools.map((tool) => ({ ...tool }))`: new promoted `EagerUse` edges) and
  `retained_instance_class_publication_test` (four report assertions). In each,
  the callback read a field of a borrowed element record or spread one, the
  row refused, and the global summary invoked the elements' stored callbacks.
  So admission must be decided before execution starts, from what the kernel
  can prove it supports for the root's whole reachable body, not from the
  root's surface shape, or the root must be attempted and rewound. Iteration
  roots take the attempted lane (`roots/body.rs`, #15262): the kernel row
  runs first and the owner keeps it only if it seals; otherwise
  `rewind_root_attempt` restores the path state, observations and the
  run-wide accumulators, and the flat lane runs as if the attempt never
  ran. The filter (`roots/operands.rs`, `root_array_iteration`) is cheap and
  has no side effects. The cost is a second execution of every refused
  attempt: in the e2e suite about 123 iteration roots attempt and about 105
  of them rewind, most in `intrinsic_array_iteration_test`. A root over
  record elements does not seal yet even when every read is covered: an
  own-data read leaves the owner's initialization domains unknown
  (`EffectCoverageFragment::lexical_operation`), so row 13 has nothing to
  seal until it does (§ Forks and joins, field reads).

  Separately, an array literal inside a callback loses its writability
  witness on the callback's second invocation (the allocation site is pooled),
  so a row nested in a kernel row refuses from the second element on and its
  root rewinds (`effect_tv_array_iteration_test`,
  `unsealed_iteration_roots_rewind_to_the_flat_lane`).

- **Other fork producers**: optional calls and member access, `if` regions.
  They reuse `LocalSuspension::Fork` and the same join.
- **The receiver-flow join** in `execution_index.rs` stays a separate
  lattice.

`World` owners that have no devel reader yet land with their first reader:

- `fallback`;
- `native.prefix_native_retained_*`;
- `classes.active_construction`;
- `descriptors.{copy_results,private}`.

## Native `Set` construction: what is left

Wave 3 row 2's `new Set(iterable)` kernel row and its `const s = new Set(items)`
roots are deferred. A `World` owner for primitive-only Set contents by
allocation (proof dependencies, joined by `World::join` like
`DenseArrays.primitive`, a new owner rather than a reuse of the array map) is
approved in principle. Starting points:

- **No Set contents model.** The legacy row returns only `Alloc(Set, site)`
  (`execute_native_resolution`: "no Set contents, size, properties or
  singleton proof"). A later `[...set]` or iteration gets its receipt only
  from `SetIterableProof::stable_spread_source` / `stable_spread_dependencies`,
  the recognizer this row replaces, so the row needs the contents owner above.
- **Constructor dependencies the recognizer supplies today.** The builtin
  `Set` row requires only `IntrinsicSlot::Global(Set)`, but the constructor
  calls `this.add` and runs the Array iterator over its argument. The legacy
  path takes `Set.prototype.add` intactness and the protected iterable
  interval from the recognizer's premise (`dependencies_for_constructor`,
  `native_iterable_operand`). A kernel row needs
  `PrototypeProperty(Set, "add")` and the Array iterator slots, in the row
  (`facts/abstract_domain/builtins.rs`, after <../docs/purity_soundness.md>) or
  checked by the adapter.
- **Open callee read.** The application adapter's `read_binding` gives the
  global `Set` open coverage and `construct()` admits only local classes, so
  a native branch must prove the callee's identity at the read (as
  `native_callee` does for member callees) and resolve the row with
  `resolve_native_call(CallKind::New, …)` over the supplied operands.

## Kernel rules waiting for their construct

- **Regions**: the kernel allocates and restores cells. The only refusal is
  `has_unscoped_lexical_bindings`. This waits for `if`/`try` region lowering.
- **Catch bindings**: `instantiate_catch_binding` and `leave_catch_binding` are
  kernel rules. This waits for `catch` lowering.

## Provider execution

Landed with Wave 3 slice 3 PR 1: the provider adapter is the one place a
provider body runs (`ProviderContext::seal_{callable,class}`), and summary
substitution is the fast path with an effect receipt
(<../docs/design.md> § The synchronous execution kernel).

What remains is running a provider body inside a caller transfer, for bodies
without a summary (a body that calls another export, writes a provider cell or
invokes an argument):

- An importer holds summaries only. The adapter needs the provider's
  `ExecutionIndex` in the importer, which is the reference head's provider
  program registry (`execution_index/provider_program.rs`) and its saved
  identities (<convergence_landing.md> Wave 3 slice 6).
- With it, the adapter gains a second constructor over the caller's fork.
  `World.scope` already decides closure addresses and literal writability; the
  heap-cell sink and the settled/writable answers in `OwnDataContext` are the
  adapter's hardcoded isolation today and become decisions on `scope`. The
  pooled-region refusal in `operation_support_receipt` lands with regions.
- Summary substitution stays the fast path tried before the adapter runs.

The flat lane `invoke_with_cells` still takes bodies with an ordered plan
behind four gates; its `CLEANUP` in `interpreter.rs` names the kernel rule each
one waits for.

## Root phases over `World` owners

`roots/{operands,body,assignment}.rs` take `&mut Executor` because the
evaluator methods they call own the whole executor. Each phase names the
`World` owners it mutates (`&mut world.publication, &world.module, ...`, as
split borrows) once those methods take split borrows themselves.
`roots/finalize.rs` already does this.
