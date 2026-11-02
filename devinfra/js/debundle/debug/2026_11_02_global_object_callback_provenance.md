# Global-object callback target provenance

An unknown member lookup on the global object used to return both `Top` and
every callback in the prefix-wide escaped pool. A call through that property
then dispatched each escaped function as an exact callable, so its effects
were recorded as `observed` even though the lookup did not establish which
function occupied the slot. The `Top` alternative already followed the
opaque-call path and attributed possible callback effects to
`opaque_callback_invocation`.

The global-object member transfer now returns only `Top` while retaining its
`GlobalAccessKind::Read` record (`facts/sync_effects/interpreter.rs`,
`Executor::evaluate_transfer`). `callback_values(Top)` still expands the
escaped pool; possible-call dispatch still reads that pool and invokes its
members under `OpaqueCallbackInvocation`. Exact function alternatives supplied
by another branch still use direct invocation and retain observed provenance.

`commuting_statements_gate_test.rs` contains a source-neutral CLI reproduction.
The fixture escapes a reader and writer through `JSON.stringify`, then makes a
guarded `window.addEventListener` lookup in one source unit and a direct write
in another. Its Node runtime short-circuits the guarded host call, with output
`5 1 1`. Before the fix, the real `spec propose-commutations` CLI emitted no
proposal and rejected the pair as an observed/observed binding conflict. After
the fix, it proposes the pair; the accepted fixture preserves runtime output
and records the possible global-call effect as fallback while the direct write
stays observed.

The separate value-flow controls retain their baseline provenance. A direct
call, an own-data method, and a genuine `Function | Top` call are observed. The
known getter read is already `fallback / escaped_callback_closure` on the
unmodified base; this change preserves that classification and does not claim
to repair or explain the getter path. The initial hypothesis that the control
fixture's callback or object exports caused that getter classification was not
supported: it remained after the fixture exported only primitive results and
was confirmed in a filtered baseline run.

No canonical graph, downstream payoff, or residual-component claim was
measured. The earlier private batch witnesses remain classification examples
in the original SCC only; their membership in the reported remaining cyclic
components is not established. The dense-array callback-promotion route is a
separate, unproven candidate and is unchanged here. A canonical attribution
would need per-call target/contributor evidence that the current batch report
does not retain.
