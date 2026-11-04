# Set owner coverage: bounded CPU audit

The saved run shows more `DebundleAnalyze` CPU, but does not identify its
cause. Source inspection identifies newly forced initializer evaluation as a
plausible contributor. It rules out executing the streamed analysis decoder
inside this action. No implementation change is justified by this audit alone.

## Compared conditions and receipts

- Previous public source: `4c6eb009d33b6dffa34e0287be0ae6dbdcd9e43c`;
  BuildBuddy invocation `358f6e91-21ba-4f90-8cf7-a027bfb44834`.
- Combined Set and streamed-reader source:
  `847a71d29c9e0f6ffca711d04b51800f3af5ee78`; invocation
  `49b0ca9e-1094-4ad2-a903-7b05b78e5b04`. Its two Set implementation
  files are identical to `604f459`, the public Set head before the unary base.
- The saved command records clean public and private checkouts, private input
  `ef70120e76ce8cc0444f107bc2401b3720d0d7c6`, RBE, `--config=nolint`,
  `--platforms=`, and successful completion. The Analyze output path records
  `k8-fastbuild`; this was not an optimized profiling run.
- The refreshed public head `ee69b9ee92b3357b42dec5ca9bb7990a7a15b96a`
  includes the merged unary allocation recognition in its base. Relative to
  the measured combined revision, `interpreter.rs` is unchanged;
  `noninvoking_initialization.rs` additionally admits logical-not and `void`
  in allocation inputs. The receipts do not measure this refreshed head.

Saved receipts are under
`/tmp/debundle-cutover-20261101/context-commutations/set-stream-engine-20261103/`:
`argv.json`, `resource-comparison.json`, `resource-summary.json`,
`executions-current.json`, and `comparison.json`. They are temporary local
evidence, not durable public attachments.

| Action                    |   Previous CPU |   Combined CPU |   Previous peak |   Combined peak |
| ------------------------- | -------------: | -------------: | --------------: | --------------: |
| `DebundleAnalyze`         | 3,607.959199 s | 5,599.337200 s | 8,217,931,776 B | 8,192,528,384 B |
| `DebundleProjectAnalysis` |   106.656542 s |    96.828786 s | 3,056,386,048 B | 3,059,621,888 B |
| `DebundleCachedPipeline`  |   258.556272 s |   259.728764 s | 7,519,420,416 B | 7,427,022,848 B |

Analyze CPU increased about 55%; execution wall time increased from 35m29.2s
to 52m15.6s. The current execution timestamps exclude input download and output
upload. The resource totals therefore establish additional CPU consumption,
not merely a longer queue. They do not establish a particular hot function,
equal worker conditions, run-to-run variance, or an isolated Set-only effect.

The saved output comparison hashes all 777 emitted JavaScript files and compares
668 graph summaries: neither changed. Effect-validation pairs fall from 11 to
9, all remaining pairs uncovered. Those are graph summaries, not proof that
every graph edge or analysis fact is identical. The reported 968 refusals moving
from `unsupported_initializer` to `initializer_result_unproved` are failed
completion attempts; the reported single owner completion must not be counted
as 969 completed proofs. These report counts do not measure execution frequency.

## New work visible in source

`OriginalInitializer::collect` now recognizes a direct `new Set([…])` with a
dense array of primitive literals. Collection scans that array and retains its
allocation identity. Previously this shape returned `UnsupportedInitializer`.

`Executor::execute_owner` chooses the dedicated initializer route for collected
owners. Its assignment loop skips unused bindings only when
`initializer_source.is_none()`. Consequently, a newly recognized Set initializer
can now request `EvaluateInitializer` even when its binding is unused. For a
needed binding, the route changes from ordinary evaluation to dedicated
evaluation. Both paths still evaluate the actual expression; the dedicated
route additionally checks initialized reads and records exact-root transfer
evidence. A failed proof returns `InitializerResultUnproved` after evaluation.
The refusal shift is consistent with more attempts, including unsuccessful
ones, but does not tell how many were previously skipped as unused.

During native resolution, the new admission helper is called after native-row
identity and arity checks. Non-Set rows fail its initial checks. An eligible Set
request checks the exact row, direct callee, dense literal shape, primitive
elements, evaluated array identity, exact-site iterable proof and dependencies.
It scans elements again and allocates a temporary vector for the singleton
value check. Exact-root transfer recording repeats the evaluated-array identity
check. Those are concrete extra operations; their CPU contribution is unknown.

The author-input fallback now follows `resolve_native_call`, so constructor
sites that formerly returned that fallback immediately attempt native resolution
first. That request clones callee, argument expressions and argument values.
The request transfers ownership of `ExecutionState`; it does not clone the whole
state. Child program points and their terminal requests/results add work to the
runtime inference machinery. Source inspection does not establish amplification
or repeated execution counts for these sites.

## Decoder boundary

`pipeline.bzl` invokes the analyzer with the projection, sources and output path.
`analyze_cli` calls `lowering::analyze_program`, which computes cross-module and
per-chunk facts and finishes with `analysis_artifact::write_artifact` and
`analysis_codec::encode`. It does not read an existing analysis artifact.

The streamed change is confined to `analysis_codec::decode`, reached through
`analysis_artifact::read_body` by stored-analysis consumers. That decoder cannot
execute on the inspected Analyze path. A rebuilt executable or changed code
layout could still affect unrelated timing; source reachability alone does not
exclude such indirect effects. The added `debundler` output group does not add
an Analyze input or a computation in its command.

## Decision and remaining uncertainty

#14118 subsequently merged with green CI. The CPU observation remains open;
the [precheck boundary](2026_11_03_set_coverage_precheck_boundary.md) records
why the first proposed shortcut was not implemented. Do not remove the dedicated evaluation, skip an iterable
guard, cache a proof across sites, or use a refusal label as a completion proof
to recover CPU: those changes could violate exact-site native integrity,
dependency transport, conservative throwing behavior, or owner coverage.
Likewise, unchanged emitted bytes do not certify general analysis soundness.

There is one bounded proposal using an existing necessary prerequisite:
`SetIterableProof::dependencies_for_constructor(exact_span).is_none()` rules out
the new primitive-array Set admission path, whose helper requires that entry
before returning success. This lookup reads the shared author dependency map
or `SemanticFacts::PrimitiveSetIteration`; it does not evaluate source again.
Presence remains insufficient: native identity, operand identity, proof use and
the final execution outcome still require their existing checks.

For that exact collected Set form only, a missing dependency entry could select
`initializer_source = None` with `InitializerResultUnproved` before requesting
`EvaluateInitializer`. Preserve the assignment loop's existing needed-binding
branch: a needed initializer must still call ordinary `evaluate` and retain its
values. An unused initializer would then skip only the new coverage evaluation.
The ordinary call/effect lanes, conservative unknown-call fallback, dependency
transport and final coverage refusal must remain active. This is a proposal,
not a verified optimization: it must establish that the skipped dedicated
evaluation contributes no effect required beyond those ordinary lanes, including
heap settling and allocation judgments. It must also establish that proof
availability is settled at this point. No evidence here counts the newly
admitted sites lacking this prerequisite or the CPU this gate would save.

One real profiler question would change the decision: **Does inclusive CPU under
`EvaluateInitializer` for newly admitted primitive-array Set owners, including
its child native requests and resulting heap/inference work, account for the
additional Analyze CPU, or does the increase occur elsewhere?** Use the existing
optimized, pre-staged `perf_wrapper.sh` workflow if measurement is authorized.
An attribution to the failed dedicated path would justify designing a bounded
early refusal that preserves ordinary needed-value evaluation and all required
effects/dependencies; this audit has not established those preconditions.

Tooling friction: aggregate RBE receipts and refusal counts cannot attribute
source-level CPU. Preserve the source identities and action receipts when
starting that profiling question, and obtain self-cost plus caller attribution
before selecting an optimization. No tests, builds, Analyze runs, benchmarks or
profiles were added or run for this audit; no BuildBuddy queries were needed.
