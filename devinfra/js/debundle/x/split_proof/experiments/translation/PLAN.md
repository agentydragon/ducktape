# Plan: from "a valid translation" to nice code with a proof

State: `translate` accepts a tree that is the bundle renamed and reshuffled, and lists **obligations**: pairs of conflicting units whose order changed. Units bind named cells (`Stmt.writes`), so a statement that assigns a variable, or changes an object, conflicts only with the ones that read or write it. The footprints still treat every call as touching everything, so almost every obligation is an effect-effect pair that an analysis could clear.

Acceptance for every step: the fixtures and mutants of `./run.sh --translation` still pass, and the step's own check shows the change on a real bundle.

## 1. Effect summaries: obligations become justified

Today every call is an effect, so most calls that merely build a value count as reordered effects.

**Design, in Lean, so that it does not break under a faint breeze.** The analysis computes a unit's `writes` and its `effect` from the code. Rules: (1) fail safe: anything not recognized touches everything, so a new bundle gets more obligations, never a wrong accept; (2) general rules about what code does, never a recognizer for an idiom or a name (no special case for a decorator helper); (3) few assumptions, kept as data, keyed by stable identities (package module and export path, never a minified name), printed with every verdict; (4) the composition rule is a theorem (`Semantics.exec_reorder`), the analysis rules are tested by the fuzzer and its mutation harness, and a rule gets a proof when it earns one.

**Measured first, on a large real bundle, with a JavaScript prototype** (not sound, not kept): effect units that evaluate to a value only, or to writes of named cells, and the pairs of conflicting units that the committed tree runs in a different order. Coarse rule: 2,590 effect units, 338,640 inverted pairs. Reading the code alone (summaries of local functions and classes, parameter-sensitive, cells for the objects they mutate): 169,977. With "intrinsics are not patched": 128,781. With a reviewed table of 46 vendor functions that only allocate: 40,180 when unknown code conflicts with every cell and every other effect (the sound rule). With "operators run no user code": 8,137. What remains is 113 units that run unknown code (method calls on module-scope objects, fluent vendor chains, calls through values) plus 32 environment effects (`window`, `document`). Resolving the class of a module-scope object (class-hierarchy method lookup) and the origin of module-scope values would remove most of the 113.

Two kinds of world: the **environment** (`window`, `console`, the DOM) orders only against other environment effects; **unknown code** (a call through an unknown value) may touch every cell and orders against everything that is not pure. An earlier version of the prototype conflated them and reported 746 pairs instead of 8,137.

The assumption "operators run no user code" (`valueOf`, `toString`, `Symbol.toPrimitive` on operands of unknown type) buys the last 96% of the pairs. It stays a printed, switchable premise until a small primitive-type domain with interprocedural parameter types replaces it.

- **Local functions:** infer a summary. Allocation and calls of pure functions are pure. A function that writes only to what it allocated is pure to its caller.
- **Outside functions:** a table keyed by (module, export, members) with a summary (`pure, fresh result`, `reads`, `writes`). `translate --table` already prints the outside imports a tree relies on, so the table starts from that list. Agents draft the entries one package at a time, in parallel. A person reviews them. The checker prints which entries it used.
- The result is a list of units that really have effects, each with the reason. Everything else commutes.

## 2. Hints for what is left

An obligation names two units. A hint says why they commute (`independent: they touch different objects`, `idempotent`) and is keyed by the units' content, not their position, so it survives a new version. The checker accepts a hint only if the facts it names hold.

## 3. Readability pass

Agents rename locals and write comments, one module cluster at a time. After each batch `translate` re-runs. Renames cost nothing to re-check because paired units only have to be equal up to renaming. Comments are not in the AST.

## 4. A new bundle version

The pairing tool matches the previous readable tree against the new bundle: units whose shape is unchanged carry over with their module, names and comments. What does not match goes to an agent, which assigns it or updates the tree. `translate` re-runs. The unmatched units are the drift report. Needs two versions of one app.

## 5. Chunks

Only the entry's export list is checked. Other chunks import from the tree. Check their imports against the tree's exports, and model several chunks as one plan (step 6 of the root [`PLAN.md`](../../PLAN.md)).

## 6. Fewer things to trust

- A scope resolver in Lean, tested against `eslint-scope` on real bundles.
- A proof that applying a bijective renaming to a plan preserves `Correct`, so the translation check implies the semantic theorem.
- Re-exports and `export default` outside the entry (rejected today).
