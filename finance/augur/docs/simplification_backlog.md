# Augur Simplification Backlog

Status: living draft. This backlog is about making Augur materially smaller for the amount
of operational behavior it implements. It is not a list of aesthetically desirable
refactors.

The central problem is the ratio of financial logic to execution plumbing. The simulator
needs vectorization, fixed shapes, one JAX scan, and explicit decode boundaries, but it does
not need every array described, allocated, copied, validated, and unpacked through several
parallel object graphs.

## Acceptance rule

A simplification PR should normally be **net negative across the whole repository diff**:
production, tests, fixtures, and build metadata together. Production-only deletion is not a
win when every caller becomes longer or a new test framework offsets it.

Before opening a PR, measure the actual branch rather than estimating deleted declarations:

- whole-tree additions, deletions, and net LOC;
- runtime production additions, deletions, and net LOC;
- which complete representation, copy, allocation, dispatch, or translation stage disappears;
- caller/test expansion caused by the migration;
- the operational behavior that remains unchanged.

Positive-net cleanup needs an exceptional, explicit justification. "Typed boundary",
"single source of truth", "cleaner abstraction", and "better structure" are not sufficient
on their own.

The active roadmap should favor removal of complete obsolete stages or representations with
roughly **100+ whole-tree LOC** payoff, preferably several hundred. Smaller changes may still be
valid incidental cleanups, but should not displace the larger deletion audit.

Do not game this rule with short aliases, reflection, generated methods, test-only wrappers,
or generic frameworks. The goal is less code and fewer moving parts, not a lower count in one
directory.

## Guardrails

- Preserve the simulator's single `jax.lax.scan`.
- Preserve financially meaningful tax, property, rent, obligation, and policy behavior.
- Keep compile-time structure in NumPy, traced values in JAX, and host decode outside JIT.
- Preserve eager compiler validation and JAX static-shape/PyTree behavior.
- Do not resume or repackage paused issue #4389's domain-transition decomposition.
- Keep public product APIs compatible unless a separately reviewed change replaces them.
- Treat dated `props/specimens/ducktape/.../code/` trees as immutable captured inputs.
- Remove deployment/config fields only with their YAML and fixture migration because config
  uses `extra="forbid"`.
- Prefer one PR that deletes an old stage over a staged migration that temporarily adds a
  second representation and never finishes the deletion.

## Evidence from completed work

The first eleven Augur cleanup PRs added **+677 repository LOC** and **+128 runtime
production LOC** in aggregate. Several improved boundaries or coverage, but the result proved
that structural consolidation is not automatically simplification.

Completed work is kept compact here; the PRs and their tests retain the detailed rationale.

| PR                                                          | Removed or established                                                        |                        Measured result | Lasting decision                                                                                       |
| ----------------------------------------------------------- | ----------------------------------------------------------------------------- | -------------------------------------: | ------------------------------------------------------------------------------------------------------ |
| [#4509](https://github.com/agentydragon/ducktape/pull/4509) | Compact calibration categorical rows                                          |                     -22 production LOC | Prefer direct construction over row scaffolding.                                                       |
| [#4511](https://github.com/agentydragon/ducktape/pull/4511) | Dead frontend residue                                                         |                    -104 production LOC | Delete confirmed unused presentation paths.                                                            |
| [#4513](https://github.com/agentydragon/ducktape/pull/4513) | Duplicate private-equity mark map                                             |                     -43 production LOC | Keep one consumed mark representation.                                                                 |
| [#4515](https://github.com/agentydragon/ducktape/pull/4515) | Propagated property `transfer_active`                                         |                     -14 production LOC | Derive mechanical fields at the read boundary.                                                         |
| [#4516](https://github.com/agentydragon/ducktape/pull/4516) | Obsolete scheduled asset-purchase path                                        |                    -629 whole-tree LOC | Delete the unused pathway end to end.                                                                  |
| [#4529](https://github.com/agentydragon/ducktape/pull/4529) | NumPy buffer/allocation/scatter mirror                                        |                    -675 whole-tree LOC | Return one dense device output tree.                                                                   |
| [#4531](https://github.com/agentydragon/ducktape/pull/4531) | Split ordinary/property cashflow engines                                      |                    -242 whole-tree LOC | Use one internal cashflow program.                                                                     |
| [#4533](https://github.com/agentydragon/ducktape/pull/4533) | Compiler re-export facade                                                     |                     -48 whole-tree LOC | Import the owning modules directly.                                                                    |
| [#4535](https://github.com/agentydragon/ducktape/pull/4535) | Property fold/remap plumbing                                                  |                     -38 whole-tree LOC | Keep the full property axis end to end.                                                                |
| [#4537](https://github.com/agentydragon/ducktape/pull/4537) | Compiler/JAX twin records for cashflow, bond, and distribution execution      |                     -51 whole-tree LOC | Preserve compiler-owned execution PyTrees.                                                             |
| [#4538](https://github.com/agentydragon/ducktape/pull/4538) | Parallel tax-breakdown output fields                                          |                     -13 whole-tree LOC | Carry one channel tensor.                                                                              |
| [#4540](https://github.com/agentydragon/ducktape/pull/4540) | Positional kernel arguments rebuilt from canonical execution records          |                     -69 whole-tree LOC | Pass typed PyTrees through kernels.                                                                    |
| [#4542](https://github.com/agentydragon/ducktape/pull/4542) | Private-equity channel adapter                                                |                     -19 whole-tree LOC | Keep decoder-only event-kind codes host-side.                                                          |
| [#4543](https://github.com/agentydragon/ducktape/pull/4543) | Repeated external-series frame walks                                          |                     -46 whole-tree LOC | Materialize indexed columns once and retain a presence mask.                                           |
| [#4544](https://github.com/agentydragon/ducktape/pull/4544) | Parallel month-zero state reconstruction assignments                          |                     -11 whole-tree LOC | Use `StateOutput` as the typed host-state field policy.                                                |
| [#4546](https://github.com/agentydragon/ducktape/pull/4546) | Generic `ProjectionRun`/Polars read-model stage                               |                    -967 whole-tree LOC | Project product/API and sweep reads directly from canonical arrays.                                    |
| [#4548](https://github.com/agentydragon/ducktape/pull/4548) | Orphaned pre-sampled private-equity trajectory overlay                        |                    -497 whole-tree LOC | Current structured providers are authoritative.                                                        |
| [#4551](https://github.com/agentydragon/ducktape/pull/4551) | Decoded Polars state-history mirrors                                          |                    -324 whole-tree LOC | Dense output is canonical; tests use explicit projection helpers.                                      |
| [#4552](https://github.com/agentydragon/ducktape/pull/4552) | Obligation compiler/JAX twin schemas                                          |                     -88 whole-tree LOC | Compiler-owned obligation records cross the JAX boundary.                                              |
| [#4555](https://github.com/agentydragon/ducktape/pull/4555) | Explicit rollout-last tensor-axis contracts                                   |                **+185 whole-tree LOC** | Correctness investment, not simplification; mypy does not prove shape strings.                         |
| [#4576](https://github.com/agentydragon/ducktape/pull/4576) | Dead strict-schema config/artifact fields                                     | -18 public source LOC; -22 private LOC | Coordinate strict deployed YAML/artifact migrations; derive VECM dimensions.                           |
| [#4606](https://github.com/agentydragon/ducktape/pull/4606) | Superseded OLS dilution fitter and CLI                                        |                    -697 whole-tree LOC | Preserve failed-model findings as evidence, not executable code.                                       |
| [#4612](https://github.com/agentydragon/ducktape/pull/4612) | Stale event-sourced design, `state.py`, local wrappers, duplicate TLH formula |                    -517 whole-tree LOC | Documentation-dominated: -518 docs, +9 runtime Python, -8 BUILD; not a gate-clearing runtime deletion. |

Failed experiments are also evidence:

- [#4525](https://github.com/agentydragon/ducktape/pull/4525) deleted 68 runtime LOC of
  `EventLog` convenience properties but added 116 test LOC and ended **+47 whole-tree LOC**;
  it was closed rather than hidden behind generated accessors.
- A broad typed-obligation plan prototype was **+166 LOC**; obligation source semantics and
  settlement metadata remain explicit.
- A first all-domain compiler/JAX consolidation prototype was **+16 LOC**; only domains whose
  canonical records actually replaced engine twins were retained in #4537.

## Historical representation map

At the post-#4538 audit checkpoint:

- `sim/engine/jax_engine.py`: about **3,874 LOC**;
- `sim/engine/jax_types.py`: about **303 LOC**;
- `sim/compiler/plan.py`: about **663 LOC**;
- `sim/output.py`: about **164 LOC**;
- `_build_program()`: about **537 LOC**;
- `_program_impl()`: about **1,330 LOC**, including one approximately 1,000-line scan step.

This checkpoint predates the state-history and obligation deletions in #4551 and #4552. It is
retained to explain the audit decisions, not as a current line-count inventory.

The remaining pipeline is:

1. product wire models lower into the authored `Scenario` domain model;
2. domain compilers assign slots, validate references, and produce NumPy execution tables;
3. `_build_program()` separates traced numeric arrays from hashable static topology;
4. one `jax.lax.scan` carries `_ScanState` and emits dense typed outputs;
5. one host boundary validates, restores month zero, normalizes axes, and decodes frames/API rows.

The large records are real but not all redundant: `CompiledSimulation` has 53 fields,
`_Operands` 32, and `_ScanState` 36. Much of `_build_program()` computes FIFO order, lifecycle
folding, policy topology, tax routing, sentinel-safe indices, and JIT cache structure. Generic
PyTree registration cannot infer those semantics.

## Current complexity verdict

The post-#4612 current-tree audit measures:

- `finance/augur/sim`: **13,122 production Python LOC** and **13,875 test LOC**;
- `finance/augur/product`: **3,022 production Python LOC** and **2,286 test LOC**;
- simulator compiler: **3,718 LOC**;
- simulator engine: **4,073 LOC**, including **3,769 LOC** in `jax_engine.py`;
- simulator codec: **1,007 LOC**; and
- authored scenario model: **1,451 LOC**.

Most simulator-core size is explicit financial-domain breadth: lifecycle/property transitions,
cashflows, obligations, funding, FIFO sales, allocation, tax and TLH, private-equity effects,
failure handling, snapshots, and event emission in financially significant order. A substantial
secondary portion is the fixed JAX topology required for dense shapes, zero-cardinality sentinels,
static gathers, cache keys, and one scan. Function size or repeated field names alone do not prove
an incidental representation.

The live handoff is one representation per necessary boundary:

1. product/API wire models;
2. product scenario translation;
3. authored `Scenario`;
4. compiler records and `CompiledSimulation`;
5. `_build_program()` static/traced lowering;
6. one `_program_impl()` JAX scan;
7. `DenseSimulationOutput`;
8. `SimulationRun`; and
9. lazy event decoding or direct product projection.

The final local core leads—`_PaymentBatch`, one-field `_SalePool`, and duplicate eager/JAX TLH
curve math—were retired in #4612. No remaining core, product/API, or frontend candidate currently
removes a coherent 100+ whole-tree LOC stage without semantic risk. Do not turn field overlap,
static-shape machinery, or the large ordered scan into a generic decomposition project.

## Standard machinery conclusion

Augur already uses the correct standard abstraction: native JAX PyTrees (`NamedTuple`,
`jax.tree_util.register_dataclass`, and `jax.tree.map`). The static/dynamic split is explicit
because it is financially and operationally significant: swept numeric values must stay traced,
while topology and feature branches deliberately form the JIT cache key.

No reviewed third-party library removes another complete stage:

- Equinox `filter_jit` can infer array versus non-array leaves, but adds a dependency and hides
  cache policy that Augur currently states explicitly.
- Flax `struct.dataclass`, Chex dataclasses, and `jax-dataclasses` mostly replace record
  declaration syntax; they do not remove compiler lowering, topology construction, or host
  normalization.
- jaxtyping and Einops improve annotations or axis readability, not representation count.
- xarray-style JAX wrappers fit analysis boundaries better than the hot scan and codecs.
- JAX `Ref` can shorten local mutation syntax, and a small `lax.scan` proof of concept passes,
  but Refs remain experimental and do not remove the functional scan-carry boundary. Do not use
  them in the production engine without a materially negative, benchmarked prototype.

The standard refactor is therefore narrower: preserve typed domain PyTrees through phase
boundaries instead of exploding them into positional arrays and rebuilding them immediately.

## Ranked remaining deletions

### 1. Retire the legacy smooth-dilution channel

The structural mint-stream model replaces smooth `(1 + r)^(t/12)` dilution whenever
`primary_round_config` is present, but the model and tests still retain the legacy smooth fallback
and its configuration surface. A complete retirement could remove roughly **1,200-1,600
whole-tree LOC** across model branches, validation, fitting assumptions, tests, and documentation.

This requires a public/private config audit first. Delete it only when every deployed and analyst
workflow uses the structural mint-stream path; do not silently reinterpret an existing smooth
configuration. Preserve the active event-driven mint formulas, seed behavior, and rollout shapes.

### 2. Retire the executable `x/pm_reifier` workflow

`finance/augur/x/pm_reifier` contains **1,957 tracked Python/BUILD LOC** of backtest, rolling,
state-space, plotting, and run harnesses. Its findings informed the current exogenous rollout
architecture, but repository references are confined to the subtree and historical design notes.

Ask for an explicit analyst-workflow retirement decision before deleting executables. Preserve the
README, generated findings, and any inputs/results that remain useful as research provenance rather
than treating the entire `finance/augur/x/` tree as disposable.

No coherent engine, product/API, or frontend deletion found by the post-#4612 audit exceeds the
100-LOC gate without semantic risk. Re-audit after these workflow retirements instead of filling
the roadmap with micro-cleanups.

## Lower-priority deletion queue

These remain valid but should not displace removal of the large execution layers:

- collapse the five parallel per-scenario frontend projection maps into one record only if
  the complete frontend/test diff is net negative and stale-request protection remains;
- remove the unused mortgage-30 evidence/config channel with YAML and fixture migration;
- derive private-equity validation code sets from their enums;
- consolidate repeated sample-sanity traversals only when the shared traversal deletes more
  code than it adds;
- audit other executable `finance/augur/x/` subtrees individually; do not bulk-delete research
  notes, captured findings, or generated evidence merely to increase the LOC total.

## Rejected directions

- deleting ergonomic APIs whose callers become larger (empirically rejected by #4525);
- broad lot-table or `CompiledSimulation` redesign without a removed stage;
- registering or tree-mapping the whole `CompiledSimulation` without an explicit static/dynamic contract;
- adopting Equinox, Flax, Chex, `jax-dataclasses`, xarray wrappers, or JAX `Ref` merely to change syntax;
- a generic private-equity channel framework;
- reflection-based buffer copying or generated dataclasses;
- a generic payment-event base class;
- a standalone universal amount-input wrapper;
- reusing `build_product_service(...)` in `profile_metric_fan.py`, which would add harvest
  policies and change profiling behavior;
- splitting `_program_impl()` into more files while retaining the same parameters, records,
  packing, and copies. File movement is not simplification.
- a generic codec frame-specification helper: codec verbosity carries semantic masks, row order,
  sparse indices, string decoding, and empty-frame behavior, while the likely payoff is a micro
  cleanup rather than removal of a representation.

## Suggested order

1. Audit public and private model configurations for any remaining legacy smooth-dilution user;
   if none remain, delete that complete fallback in one coordinated PR.
2. Ask for an explicit analyst-workflow retirement decision for `x/pm_reifier`; if retired,
   delete its executable harness while retaining useful research provenance.
3. Re-audit the remaining compiler/JAX and artifact/config surfaces for another complete obsolete
   stage with roughly 100+ whole-tree LOC payoff before dispatching smaller cleanups.

Re-audit actual branch deltas after each change. If a proposal expands the tree, stop before
opening a PR and record the failed experiment here.
