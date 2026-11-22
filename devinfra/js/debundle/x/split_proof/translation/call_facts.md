# Per-call constructor facts

`call_facts` inspects direct calls in top-level variable initializers to one named, uniquely declared module-scope function. It reports constructor declarations in the helper's eagerly evaluated body, relevant class field and constructor-property syntax, each call's object-literal properties and eager argument operations, and explicit unresolved conditions. It uses scope resolution to exclude shadowed identifiers and reports writes to resolved bindings anywhere in the source.

It does **not** decide purity, prove property descriptors, evaluate helper or constructor bodies, analyze alias calls or other call sites, or account for the emitted module order. Unknown calls and prior external code can change relevant state; syntactic name resolution and counts do not establish a runtime binding or prototype invariant. Constructor reads/writes and eager operations are partial syntax observations, not exhaustive effect summaries. The checker is unchanged; no returned value is an acceptance verdict.

Run against a local bundle, keeping private inputs and output private:

```sh
bb run //devinfra/js/debundle/x/split_proof/translation:call_facts -- /path/to/bundle.mjs helperName > facts.json
```

The focused test uses an actual inherited-getter Node counterexample in addition to checking scope, unknowns and constructor facts. It does not prove JavaScript semantics.
