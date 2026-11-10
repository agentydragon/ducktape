# Ordered execution: soundness traps

Traps met while moving the synchronous interpreter onto one ordered executor
shared by the application and strict-provider adapters. The current rules are
in <../design.md> § At-init call promotion and § The synchronous execution
kernel; this note records the failure each rule prevents, so a later
"simplification" does not reintroduce it.

## Evaluation order and results

- **Never recover a value by evaluating again.** An effects pass followed by a
  value query runs a stateful call twice. A call transfer returns effects and
  value together. A complete effects-only transfer (a host timing assertion, an
  audited contract without a result model) supplies no value: the caller
  declines with `missing_call_result` and never retries the source evaluator.
  Helpers that evaluate source operands internally (array iteration,
  from-entries) cannot take already evaluated operands.
- **A source-call result cache has the wrong scope.** Keyed by call site it
  conflates activations, constructor/model activations and hypothetical value
  queries; adding the prefix revision makes stateful calls miss after their own
  writes. Transport operands and results explicitly instead.
- **Select support before evaluating children**, so effects are never executed
  and then rolled back for a fallback. A failed ordered body never replays
  operations it already executed.
- **Pre-evaluated locals go stale.** Caching a primitive before flattened calls
  let skipped code replace it with an object. Every incomplete transfer widens
  the local aliases it may have reached.
- **Unfinished literals need roots.** When a later field falls back before the
  record exists, earlier field values must stay reachable as fallback inputs,
  even after their local aliases change.

## Heap identity

- **An allocation site is not an allocation.** The heap keys records by source
  site. A site already in the heap, reachable through inputs or external reads,
  or reentered by a child (`{ value: make() }` where `make` allocates at the
  same site) also denotes older objects, so it gets no fresh-record proof.
  Distinct invocation/allocation generations are a prerequisite for relaxing
  this.
- **Calls borrow what they reach.** Records reachable from the callee, receiver
  or arguments become borrowed before invocation; a completed callee may have
  changed them.
- **Skipped code is not escape.** Tainting every object an incomplete statement
  read made later own-data reads lose completeness
  (`synchronous_precision_report_test::incomplete_local_call_preserves_unrelated_own_data_reads`).
  `unsettled_objects` (possibly changed by skipped code) and `tainted_objects`
  (escaped to unknown code) have different consumers, and
  `unknown_array_contents` invalidates values without implying a possible
  accessor. Conversely, a receiver changed by unknown code may now hold a getter,
  not only stored callbacks.
- **A cell read runs no code; a property read may.** Copying a local
  declarative binding invokes no getter or coercion, so it needs no freshness
  proof. A host global may be an accessor, and `{ value: source.current }` needs
  a getter-aware property transfer.
- **Publication does not replace `[[Call]]`.** A published ordinary source
  function remains a valid singleton call target, but its cells and reachable
  heap gain no immutability. Tainted functions bypass invocation memos because
  heap taint can change without advancing the prefix or reachability revision
  in the memo signature. Other memo callers have not been audited for heap,
  descriptor or native-state invalidations the signature misses.
- **An opaque carrier proves no shape.** A callback-retention carrier around a
  foreign factory result does not establish an audited factory's accessor-free
  input precondition. Before `DeferredCallbacks` and `AuditedRetainedResult`
  were separated, the CLI accepted a split that turned a caught TDZ read into
  `42` (`foreign_call_summary_test::opaque_result_does_not_certify_an_audited_factory_input`).
  Revocation keys are interned capture sets; key equality is never JavaScript
  object identity.

## Closures, cells and memory

- **Snapshots cannot see later writes.** In
  `function f(a, b) { let s = a; const r = () => s; s = b; return r; }` the
  closure must observe `b`, and two calls need distinct cells. Each activation
  allocates monotonic cell IDs that are never reused.
- **Every callable form needs its own body coverage.** Concise arrows first had
  no ordered body and silently used the source-summary path, which invalidates
  cell precision. Recognizing closure creation does not prove its body runs on
  the ordered path; nor does a native global read pass the body's free-read
  entry check just because its member operation is supported.
- **Captured `var`s may alias arguments.** A set of capture names cannot prove
  opaque use harmless; the returned carrier keeps all argument identities. The
  execution index's `current` effect owner is inherited by accessors and static
  blocks, so it is not the declaring `var` environment.
- **Collect only outside transfers.** A suspended caller's Rust frame holds
  arguments, captures and results that are not in `ExecutionState`. Adding a
  value-bearing state field means extending the collector's root list.
- **Revocations must own their description.** With weak capture interning, a
  revocation that kept only a numeric identity let an equal description created
  after reclamation get a new identity and appear unrevoked.
- **Source positions are chunk-local.** Each chunk parses into its own source
  map; never look up a donor chunk's raw offset in an importer's execution
  index.

## Completeness is not purity or coverage

- `Outcome.complete`, empty binding/heap cells, unchanged interpreter state and
  empty havoc (possibly cleared by author policy) establish neither evaluation
  purity nor effect-domain coverage. The synchronous summary has no
  callee-global, general-property or host-queue access sets.
- An effect labelled `observed` is a non-fallback abstraction, not an execution
  trace. Reduce the source and compare against a real-read control before
  expanding a model or strengthening a premise.
- A pair-specific premise needs per-pair participants: if source 1 writes `c`
  and sources 2 and 3 read it, emitted order 3, 2, 1 still exposes (1, 2) after
  a premise covers (1, 3). Keeping one maximum participant per cell hides it.
- Node's `global` is a census alias but not an execution global-object name, so
  its member accesses stay opaque.
- A target-local mutation contract does not establish callback timing. A
  member-decorator helper can call the method immediately; class and prototype
  surfaces must stay in callback discovery, and promoted class-method rebinds
  must update prefix values.

## Fixture traps

- Selecting all bindings is not complete source-unit ownership: anonymous
  statements stay residual and can form a genuine cycle before the property
  under test is checked. Co-locate them explicitly.
- Build author inputs as typed values (`IntrinsicFamily::Array`), not
  hand-spelled strings that fail serialization.
- An unused premise contributes no dependency; a control must use the shape
  that actually consumes its assertion.
