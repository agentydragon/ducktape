# Plan: from "a valid translation" to nice code with a proof

State: `translate` accepts a tree that is the bundle renamed and reshuffled, and lists **obligations** for effectful units whose order changed. Nothing justifies those obligations yet.

Acceptance for every step: the fixtures and mutants of `./run.sh --translation` still pass, and the step's own check shows the change on a real bundle.

## 1. Effect summaries: obligations become justified

Today every call is an effect, so most calls that merely build a value count as reordered effects.

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
