# Recursive Purity Backlog

This note tracks reusable debundler purity-analysis work. It excludes
corpus-specific owner ids, bundle paths, and spec cleanup notes.

## Remaining Generic Follow-Ups

### Statement-Level Purity Override

`purity: pure` on a member is callsite-oriented: it says calls to that binding
are safe. It does not assert that the binding's own owner statement is pure.

A separate statement-level override, tentatively `owner_purity: pure`, would
let an author assert that a specific owner statement is safe when the analyzer
cannot prove it. This should remain rare. Prefer analyzer improvements for
common expression families.

### Redundant-Hint Guardrail

The redundant-hint side output can support a CI or pre-commit check that fails
on new redundant `purity: pure` hints except for an allowlist of intentional
overrides. This is prevention, not analysis.

## Design Principle

The goal is compositional proof, not broad allowlists. For expressions such as:

```js
const value = new Set(["literal", ...(isEnabled() ? ["extra"] : [])]);
```

the classifier should prove purity from the leaves upward:

- primitive literals are pure
- the conditional is pure if the predicate and branches are pure
- spreading a pure array into an array literal is pure
- constructing a `Set` from a pure iterable is pure
- a helper call is pure if the helper body and arguments are pure

Manual overrides are for genuinely safe-but-not-provable cases, not for normal
helper chains.
