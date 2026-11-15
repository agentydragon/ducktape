# Host setters observe initialization order

The final ESM schedule guard closes an unsound accepted split independently of
native-call purity. A host may install a setter before loading the program:

```js
Object.defineProperty(globalThis, "saved", {
  set(callback) {
    console.log("setter", callback());
  },
});
```

The source initializes `later = 0`, creates `callback = () => later`, publishes
it with `globalThis.saved = callback`, then assigns `later = 42`. Moving the
`later` declaration and assignment to a dependency of the publication module
changes the setter's observation. In the actual emitted cycle the setter throws
`ReferenceError` because `later` is still in its TDZ; another dependency order
could expose `42` instead. Ordinary Node startup without
the setter misses this divergence: both versions later print `264 42`.

`evaluation_schedule_test::a_host_setter_observes_the_original_captured_cell_before_its_later_update`
runs the real CLI and Node with that host. Normal emission refuses with
`evaluation_schedule_unknown_reordered` and a typed `evaluation_schedule.json`.
`--emit-on-rejection` writes a marked diagnostic tree and exits with rejection;
executing that tree proves the divergent observation, not equivalence.

The same issue affects a published `() => factory()` before a later class:
ESM dependencies can initialize the class before publication, replacing a source
TDZ error with a class value. Certifying a selected `Symbol`, `Set`, or
`TextEncoder` operation does not certify a different global property's setter,
its callbacks, or their initialization phase. `native_operation_input_test::a_selected_native_call_does_not_authorize_a_changed_host_setter_phase`
executes that exact selected-Symbol counterexample: the source setter catches
`ReferenceError`, while the rejected emitted setter observes `LaterError`.
Native-operation positive fixtures preserve publication/class ordering while
retaining selected-site provenance and unselected-site graph controls. A separate
native-only inversion moves Symbol construction across private lexical stores;
its console observation stays after both modules.

This is part of [cross-chunk parity](../plans/cross_chunk_parity.md) and the
[runtime source-identity investigation](2026_11_15_runtime_source_identities.md).
Qualified live cells identify the right value; final schedule validation must
also preserve when escaped code can observe that value.

An unchanged absolute import can also change the evaluated module graph. An
unindexed `external.mjs` may import the original `snapshot/main.js`. The source
cycle evaluates `external` then `entry`; after relocation, the same `file:` URL
loads the original entry in addition to the emitted entry and prints `entry`,
`external`, `entry`. The real CLI/Node test
`evaluation_schedule_test::an_unindexed_absolute_file_backedge_cannot_reload_the_original_entry`
checks that divergence in marked rejected output. Generic URLs and absolute
files require an indexed closure or existing vendor equivalence; immediate
specifier equality is insufficient. Recognized Node builtin spellings remain
closed host identities, including bare `path`, `assert`, and `async_hooks`.
