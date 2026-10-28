# Combined inference migration

## Objective and completion requirements

Implement the full clear-path cohort previously estimated as roughly 70% of the
remaining semantic inference logic, inside native GitHub stack #12712.

- Model the cohort as one typed dependency graph. Every mutually dependent
  region shares a fact store and dependency worklist. A completion barrier may
  partition the graph only at a documented, strictly one-way dependency.
  Sharing a scheduler implementation alone does not integrate feedback.
- Mutually dependent rule families must iterate together. Changes to a fact must
  wake consumers in other families without rebuilding a private summary store.
  Completed immutable predecessor facts may cross a justified one-way barrier;
  retain their exact proof dependencies and source identity.
- Retain a barrier only where the actual dependency is one-way: X can trigger Y,
  and Y cannot feed facts back into X. Record the producer/consumer dependency
  evidence for each retained barrier. An existing phase boundary alone is not
  justification.
- Build recursive purity bottom-up from growing effects and call obligations.
  An empty provisional effect set is not a completed purity certificate.
  Preserve pure recursive cycles, impurity propagation, exact author-input
  dependencies, pinned author assertions, and finite diagnostic causes.
- Keep source/runtime assumptions explicit. Source-only validation must complete
  before author inputs can discharge distinct obligations; never manufacture
  native absence from an unfinished census or missing positive fact.
- Preserve observable output and conservative fallback. Use reference
  differentials, negative source controls, CLI/Node tests, scale checks, and
  explicit analysis-library lint/build validation of the combined head.
- Publish focused PRs and verify native stack membership and exact-head CI.
  Existing domain-local ports are intermediate work, not completion evidence.

## Cohort inventory

The implemented ports provide typed transfer functions for the combined graph.
Replace separate solvers within feedback regions with shared registrations;
retain a one-way boundary only with the evidence below:

- Binding/property/return/parameter value flow and candidate-object reachability.
- Global escape taint and Set iterable alias invalidation.
- Fluent and plain-array candidate derivation and source-ordered primitive/Symbol
  const certificates, retaining completed write/escape/method vetoes.
- Native invalidation, intrinsic receiver aliases before/after author inputs,
  prototype capability containment, namespace aliases and receiver aliases.
- Namespace capability rejection and dependent fresh-result write premises.
- Source-unit read/rebind closure and static prefix binding/object retention.
- Local primitive-binding import requirements and primitive import/re-export closure.
- Local and cross-module recursive purity.
- Existing native/construction/callable semantic certificates and call-local
  property proofs, with their context/lifetime boundaries made explicit.

Source pattern collectors and AST visitors remain canonical fact producers.
Execution-prefix heap/capture/reentry interpretation, opaque loader return-shape
inference, context invalidation across changed programs, and unbounded recursive
summary generalization require separate designs. A deferred item must not hide a
clear source-only migration from the cohort.

## Integration acceptance checks

1. Keep the audited dependency graph below current as transfers change.
2. Provide typed registrations/projections within each recursive region; avoid
   untyped string keys and separately materialized authoritative fact maps.
3. Flatten function effects and imported-call obligations into bottom-up rules
   across module boundaries; support completion without optimistic certificates.
4. Replace mutually dependent domain-local solves with shared registrations and
   justify every surviving one-way barrier from concrete code dependencies.
5. Run combined validation, review resource behavior, and update each PR's scope
   and validation statements to reflect the final implementation.

## Audited one-way boundaries

These are source-code dependency claims, not a claim that the final integration
has passed validation. Re-audit a boundary if a consumer begins producing facts
used by its predecessor.

| Producer                                                        | Consumer                                                              | Evidence and completion requirement                                                                                                                                                                                                                                                    |
| --------------------------------------------------------------- | --------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Binding/property/return/parameter flow                          | Candidate reachability and structural value shapes                    | `facts/analyze.rs` and `value_flow.rs`: candidate and shape consumers read flow; they never change flow. Candidate reachability should nevertheless read the authoritative flow store directly.                                                                                        |
| Source-only aliases and assignments                             | Captured author-input validation, then author-assisted aliases        | `execution_index.rs`: source-only prototype/native absence checks must not consume the assertions they validate.                                                                                                                                                                       |
| Complete conditional native-write census from every chunk       | Native integrity and semantic certificates                            | `SemanticRules::derive` checks census completion. Branch refinement consumes source-only `callback_object_arguments`; post-integrity factory certificates do not produce that field (`callback_arguments.rs`, `CallableSummary::callback_factory`). Temporary absence is insufficient. |
| Native integrity and canonical shapes                           | Native identity, collection, class, callable and wrapper certificates | `semantic_inference.rs` and `callable_inference.rs` compose positive premises. These certificates do not alter source shapes or the census.                                                                                                                                            |
| Completed source/semantic certificates                          | Function effect and call-obligation closure                           | `classifier.rs` consumes those proofs. Function purity does not feed value flow, aliases or the native census. Local and imported function effects are mutually dependent and must share one closure.                                                                                  |
| Static read/rebind closure and completed function certificates  | Execution-prefix heap/capture/reentry interpretation                  | Runtime state is position- and call-specific. It never updates the static closure or function-purity cache.                                                                                                                                                                            |
| Completed semantic proofs and the selected current prefix state | Call-local own-property projections                                   | `property_inference.rs` borrows current shape/state and discards the query store after the call. A later mutation cannot reuse it. No prefix projection certifies a provider source fact.                                                                                              |

Purity analyses with distinct author policies are distinct logical contexts:
chunk-local declared-pure hints must not bless export inference. Pinned program
export assertions remain explicit axioms. Mode-distinct function identities keep
those contexts from certifying one another accidentally.
