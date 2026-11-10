# Recursive Purity Backlog

Design follow-ups for <../TODO.md>, not a separate priority queue. This note
tracks reusable debundler purity-analysis work. It excludes
corpus-specific source-unit IDs, bundle paths, and spec cleanup notes.

## Remaining Generic Follow-Ups

### Statement-Level Purity Override

`purity: pure` on a member is callsite-oriented: it says calls to that binding
are safe. It does not assert that the binding's own source-unit statement is pure.

A separate statement-level override, tentatively `owner_purity: pure`, would
let an author assert that a specific source-unit statement is safe when the analyzer
cannot prove it. This should remain rare. Prefer analyzer improvements for
common expression families.

### Redundant-Hint Guardrail

The `modules.json` redundant-hint report identifies when a `purity: pure` hint
appears redundant. Add a CI or pre-commit check for new redundant hints, with a
narrow allowlist for intentional overrides. This is prevention, not analysis.

### RegExp literal admission

Keep literal `RegExp` construction conservative until static ECMA-262 pattern
validation proves the constructor cannot throw. The deferred classifier case
and its current boundary are recorded in <../TODO.md#purity-classifier>.

## Design Principle

The goal is compositional proof, not broad allowlists. For expressions such as:

```js
const value = new Set(["literal", ...(isEnabled() ? ["extra"] : [])]);
```

the classifier should prove purity from the leaves upward:

- primitive literals are pure
- the conditional is pure if the predicate and branches are pure
- spreading requires a statically safe iterable shape, not just a pure-valued expression
- constructing a `Set` additionally requires the classifier's constructor and iterable preconditions
- helper calls need the applicable body/argument and binding-resolution proof

These are proof obligations, not proposed blanket whitelist rules. A pure
expression can evaluate to an object with an effectful iterator or getter.
The exact admission rules and intrinsic-integrity assumption are documented in
<../docs/purity_soundness.md>.

Manual overrides are for genuinely safe-but-not-provable cases, not for normal
helper chains.
