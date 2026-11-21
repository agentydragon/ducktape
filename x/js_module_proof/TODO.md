# Module-splitting fuzzer follow-ups

The aim is to test the real splitter and validator against Node on a much wider
range of programs and splits. The fuzzer supplies test inputs and observations;
it is not another formal JavaScript model. A finite test run cannot prove
correctness for all JavaScript.

## 1. Generate broader valid programs

The closure generator now tracks lexical bindings and independently runs nested,
escaping closures in Node, including parameters, shadowing, captured writes,
bounded loops and branches.
Incrementing-counter closures and factories producing parameterized closures
with independent captured numeric cells can also be split and validated against
the shared Lean semantics. Immutable alias chains share those cells; the fuzzer
varies inputs and module assignments for these cases.
The generator also exercises restricted nonescaping objects, arrays and alias
mutation in Node and the Lean-backed checker. The following broader
generator and syntax work remains open.
An opt-in compositional generator now builds numeric expressions, nested
branches, declarations and local mutations from visible scopes; independent
split mode also samples acyclic graphs, cycles and invalid assignments. These
cover only part of the grammar and split space described below.

- [ ] Replace the fixed collection of supported statement shapes and injected
      unsupported nodes with a compositional, scope-aware ESTree generator. Track
      declarations, references, initialization and control-flow context so generated
      programs parse and run, while varying nesting, expression depth, names and
      declaration order. Report generated construct coverage.
- [ ] Add productions incrementally for lexical and function scopes, closures,
      parameters and returns; expressions and control flow; objects, arrays and
      property access; mutation and aliases; exceptions and `try`/`finally`; and
      imports, exports and module cycles. Include asynchronous features only once
      the harness can observe their completion deterministically. Exercise both
      supported and unsupported syntax without treating refusal as a comparison.
- [ ] Bound recursion, loops, allocations and execution time. Generate host
      interactions through a deterministic harness, and classify timeouts, resource
      exhaustion and unsupported host behavior as inconclusive rather than equal.
      Test the generator by parsing and executing its output independently of the
      splitter.

## 2. Explore splits independently of program generation

- [ ] Generate varied statement and binding partitions, module graphs and import
      orderings, including empty modules, cycles, missing edges and cross-module
      writes. Include both plausible valid inputs and deliberately invalid specs;
      record which category was requested and what was actually exercised.
- [ ] Separate unsupported syntax, invalid assignments, splitter failures and
      validator `unknown` results in the report. Unsupported or invalid inputs must
      never be reported as proved; do not require refusal for a construct merely
      because it was unsupported when the test was written.
- [ ] Keep exercising altered emitted modules, including changed bindings,
      imports, exports, effects and function bodies. Report their results separately
      from unmodified splitter outputs.

## 3. Make the differential oracle useful at scale

- [ ] Run source and actual splitter output in isolated Node processes with the
      same controlled inputs and module-loading assumptions. Compare initialization
      traces, exports, return values, thrown values and later observable effects;
      preserve the trace prefix on failure. Define how exceptions and asynchronous
      observations are normalized and how nondeterministic programs are excluded.
- [ ] For each validated pair, fail on a Node counterexample to a proved result
      within the proof's assumptions; independently reproduce every reported unsafe
      witness. Record Node differences for `unknown` or unvalidated cases as test
      evidence, not as a proof of a validator bug or a successful proof.
- [ ] Preserve seed, case index, source, assignment, emitted files, observations,
      verdict and relevant runtime versions for failures and interesting differences.
      Automatically minimize unmutated proved-versus-Node counterexamples by
      removing statements, calls, empty modules and imports while preserving that
      predicate. Binary expression subtrees can also shrink to either child;
      extend shrinking to other failure categories, more expression forms and
      split transformations, and promote minimized
      bugs to regression tests.
      Mutated proved-versus-Node counterexamples now use the same reducer.
- [ ] Track grammar, split, verdict and observation coverage, including refusal
      and timeout rates. Support deterministic sharding and replay; run a bounded
      representative suite in CI and a configurable larger campaign separately.
      Keep Node-only and Lean-validated counts distinct.

## 4. Grow the proof-supported subset in vertical slices

- [ ] Pick constructs from the generated corpus, extend the existing shared
      semantics and frontend together, and test each slice end to end against Node
      on accepted, unsafe and unsupported examples. Avoid adding independent formal
      models for the fuzzer or for individual syntax features.
- [ ] Extend renamed local imports beyond simple readers, records, calls, local
      addition updates, and pure numeric expressions with parameters.
      The generator and an axiom-free acceptance certificate cover these cases,
      including relayed imports; the AST fuzzer also redirects reader imports to
      check unsafe witnesses.
      Renamed reexports through
      non-entry modules now retain their declaration identities in Lean and can be
      generated and checked. Redirected reexports used by readers and restricted
      numeric expressions have Node-reproduced Lean counterexamples, including
      initialization observations. Witness lowering alpha-renames locals to
      preserve their identities when redirected module bindings share a spelling.
      Witness lowering follows actual callee bodies in their own module scope,
      including changed callees with extra arguments or unused missing parameters;
      extend it to observed missing arguments, captured closures and other statement forms.
- [ ] State and test each slice's observation boundary and assumptions, including
      module loading, host behavior, numeric semantics and termination. Keep
      `unknown` when the checker cannot justify its claim, and document that Node
      fuzzing tests but does not prove the model's correspondence to ECMAScript.
      The frontend's conservative numeric bounds are currently trusted code;
      formalize and prove their relationship to execution, including sequential
      effects in operands and arguments and redirected callees' argument and
      return ranges, or replace them with a proved analysis. Lean-resolved
      witness call graphs narrow widening for supported functions; extend that
      precision to captured closures and graphs that currently fall back to a
      whole-program bound.
- [x] Model observed and thrown JavaScript strings as UTF-16 code-unit sequences
      in the shared semantics, including unpaired surrogates. Extend the frontend,
      Node witness encoding, certificates and generated tests together.
- [ ] Add real-world JavaScript fixtures as they become useful, alongside
      generated cases, and measure how often real assignments can be checked. Do not
      weaken the proof claim simply to increase the acceptance rate.
      `real_corpus.mjs` now measures three candidate assignments per input and
      reports generation refusals and validation unknowns by reason; expand the
      corpus and investigate the most common reasons for unknown. The current
      browser fixture reaches `document.querySelector(...)` after its discarded
      directive; modeling that requires host calls and reference-valued results
      in the shared semantics, including their effects and observable identity.
