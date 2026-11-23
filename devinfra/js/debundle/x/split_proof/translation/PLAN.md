# Plan: from "a valid translation" to nice code with a proof

Work remaining for the [translation validator](README.md).

Acceptance for every step: the fixtures and mutants of `:fixtures_test` still pass, and the step's own check shows the change on a real bundle.

## 1. Effect summaries: obligations become justified

The [bundle measurements](../experiments/README.md#13-footprints-by-abstract-interpretation)
record obligation counts, vendor experiments and cost. Open work:

- **Reviewed vendor table.** Key entries by package and export path. Draft one package at a time, review each entry and report which entries the checker used. `translate --table` lists the rewrites a tree relies on.
- **Decorators and registrations.** Establish which targets and registries each vendor helper can change before allowing applications on different targets to commute (§ 2).
- **Cost and budget.** Profile before raising the step budget. Exhausted units must fall back to unknown code. [Measured costs](../effects/README.md#cost)
- **Proved rules.** Use the [footprint certificate plan](../docs/footprint_soundness.md) for the interpreter proof work. Prioritize rules that failed audits, including deferred or skipped calls.
- **Premise P3.** Check whether a bundle's world can install accessors or conversion hooks on escaped objects before relying on P3. An observable-state library defining accessors on instances can invalidate it. [Premises](../effects/README.md#premises)

## 2. Hints for what is left

An obligation names two units. A hint says why they commute (`independent: they touch different objects`, `idempotent`) and is keyed by the units' content, not their position, so it survives a new version. The checker accepts a hint only if the facts it names hold.

## 3. Readability pass

Agents rename locals and write comments, one module cluster at a time. After each batch `translate` re-runs. Renames cost nothing to re-check because paired units only have to be equal up to renaming. Comments are not in the AST.

## 4. A new bundle version

The pairing tool matches the previous readable tree against the new bundle: units whose shape is unchanged carry over with their module, names and comments. What does not match goes to an agent, which assigns it or updates the tree. `translate` re-runs. The unmatched units are the drift report. Needs two versions of one app.

## 5. Chunks

Only the entry's export list is checked. Other chunks import from the tree. Check their imports against the tree's exports, and model several chunks as one plan (step 6 of the root [`PLAN.md`](../PLAN.md)).

## 6. Fewer things to trust

- A scope resolver in Lean, tested against `eslint-scope` on real bundles.
- A proof that applying a bijective renaming to a plan preserves `Correct`, so the translation check implies the semantic theorem.
- Re-exports and `export default` outside the entry (rejected today).

## 7. A generator that can reach any program, and a footprint oracle

Extend the inputs to the existing [footprint oracle](README.md#the-footprint-oracle).
Use the [coverage audit](COVERAGE.md#gaps) to find unevaluated Effects cases and
uncalled intrinsic summaries. Remaining syntax includes export forms, `using`,
top-level `await` and JSON import attributes. The translation checker refuses
default and source-bearing exports, and the oracle refuses top-level `await`.
Teach the fuzzer's splitter to emit an aliased export before testing alias placement.

Save a wrong footprint as a runnable [finding](debug/generator_findings/README.md)
and add a case to [`effects/cases_general.mjs`](../effects/cases_general.mjs) when
the analysis refuses the unsafe permutation.
