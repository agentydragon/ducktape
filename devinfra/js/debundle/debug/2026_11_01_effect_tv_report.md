# Initial effect-set translation-validation report

## Audit boundary

The staged analysis artifact already retains `StatementFacts`, including its
private synchronous-call and heap summaries, with restored hygienic binding
identities. S4 still owns those facts when constructing the assignment's
reports. A facts-backed report therefore needs a public typed projection,
not another AST pass or a graph-report reconstruction.

`StatementFacts::effects()` is insufficient for initialization semantics: it
omits synchronous callee accesses and provenance, and includes function
declaration initialization. The binding timeline already has the correct
instantiation guard. Share that guard while leaving graph behavior unchanged.

The retained synchronous summary is not a complete arbitrary-effect summary.
It has binding reads/rebinds and array/Set contents, but no callee-global,
general-property or host-queue access sets. Its `complete` field does not
certify these missing domains. Pure expressions can also observe mutable
property state. Legacy timing havoc may have been cleared by existing author
policy, so empty havoc is not a new coverage certificate either.

## Conservative projection

The report projects eager accesses and synchronous callee binding effects at
the calling statement. Fallback-only reasons survive, and observed access
wins a duplicate fallback contribution. Own heap contents are produced by
the existing collector's eager bucket. Called bodies' heap contents are
composed separately into `SynchronousCalls`; these lack per-access provenance
and stay explicitly unclassified in the projection.

Allocation sites are retained as conservative write-like events, without a
freshness, disjointness or heap-locality assertion. Target-local mutation
roots are not reinterpreted as binding reassignment.

Unknown global/property/host coverage is represented separately from timing
opacity. It remains global Havoc for runtime evaluation statements, even
when execution completes or purity is inferred. Instantiated function and
import declarations execute no body here. Export kind alone is insufficient
to establish that fact; default expressions and classes execute code.

This deliberately broad starting point exposes missing coverage. It does
not establish a precise effect-only acceptance predicate.

## Pair-specific premises and bounded output

Real cells can have many writers/readers. Keeping one maximum participant is
insufficient once an author premise covers a particular pair: source 1
writes c, sources 2 and 3 read c, and emitted order 3,2,1 must still expose
(1,2) after a premise covers (1,3).

The sweep queries ordered source-ordinal suffixes of already-run readers
and writers. It merges those suffixes by partner using a heap, aggregating
one pair's cells at a time. Havoc is another wildcard participant range,
including conflicts with read-only effects. Pair dispositions never mutate
these participants. Observed conflicts contradict an existing premise in
the new report; they do not create a new hard rejection.

Traversal is output-sensitive, including a logarithmic iterator-merge cost.
Exact counts cover all pairs, cell incidences and dispositions. Default JSON
retains twenty pair examples and twenty cells per sampled pair, with exact
cell counts and explicit pair/cell truncation. It retains neither a global
pair matrix nor the current statement's full partner/cell matrix.

The initial tranche had no full-detail CLI export. The follow-up
`--effect-conflicts-jsonl` now streams full rows through the same fallible
visitor, including endpoint opacity and every cell's provenance. The bounded
summary retains the same limits. This remains report-only TV2; effect-domain
coverage and canonical comparisons are incomplete. Legacy gate decisions,
forward-edge handling, placement checks and the edge-TV oracle are unchanged.

## Forward-read control from the canonical audit

Engine `2f0383ee465ac00eb8aab4de04bddd67c1555774` with the spec-only apiErrors
forward-port retained the same original 75 graph edge multisets, while
restoring the 76th graph. Its accepted `mcp` and `remoteWorkerWire` reports
contain 30 and 6 swapped forward eager reads respectively. Every read site
has incomplete synchronous execution with global precision losses. Each
pair's reverse binding timeline is inerted by an existing author premise;
the read-side evidence is fallback, not an interpreter absence certificate.

The report must expose these raw binding conflicts and existing premise
dispositions. It must not infer that deferred callbacks cannot execute merely
because the old gate accepts. Future approved provider/no-reentry derivation
may improve interpreter facts; changing the TV classifier is not that proof.

## Validation

Focused checks use external CLI fixtures and the existing staged roundtrip.
They cover the pair-specific override trap, hoisted declarations versus
function expressions, missing global/host coverage with completed execution,
and exact counts with bounded output. The staged check explicitly requires
the new report before comparing direct and artifact-backed output trees.

An initial check compiled the implementation and passed staged roundtrips;
three new fixture assumptions needed correction (an unused pair premise,
an assumed cyclic-loader orientation, and guessed literal import order).
These did not justify changes to gate behavior or semantic premises.
The corrected override control uses a direct write and two read-only callback
bodies. Its cyclic module names make the writer the first entry import, so
DFS visits both reader bodies before that writer. Both cell conflicts remain
visible, with only the specified writer/reader pair covered by the premise.

Final command, in the Nix devshell with normal RBE defaults:

```sh
bazelisk test \
  //devinfra/js/debundle/e2e:effect_translation_validation_test \
  //devinfra/js/debundle/e2e:staged_analysis_test \
  //devinfra/js/debundle/e2e:translation_validation_differential_test \
  --test_output=errors
```

[BuildBuddy invocation](https://app.buildbuddy.io/invocation/3b5c2d5f-fec1-4f00-8c26-006bb906a08b):
all three targets pass (4 effect controls, 4 staged controls, 5 legacy
differential controls). The staged target was reused from the immediately
preceding passing execution with the same code inputs. No canonical effect-TV
comparison or full-detail export validation is claimed by this focused run.

## Streaming follow-up

The follow-up adds `debundle run --effect-conflicts-jsonl`. The existing
ordinal-suffix sweep invokes an optional fallible borrowed row visitor; both
bounded examples and JSONL use the same cell iterator. Without an active
sink, statement/module metadata is constructed only for the bounded examples.
The stream includes every cell, typed provenance and disposition, existing
premise locations, legacy-pair membership, and both endpoints' typed opacity.
It retains one candidate pair's cell map at a time. Serialization, newline,
and explicit buffer-flush errors propagate before writing the summary.

Repeated `--effect-conflicts-chunk <CHUNK>` arguments require the export flag
and reject unknown materialized chunk names. They select only S4 JSONL sinks:
the full configured input, selected import closure, census, staged analysis,
graphs, emitted order, and every bounded report remain unchanged. This allows
the canonical `mcp` and `remoteWorkerWire` streams to be inspected without
writing the much larger Tana stream. Existing dry-run policy is authoritative:
rejection reports can include JSONL; a policy that suppresses the requested
sink produces an explicit error.

The same three-target command above passes on devel base
`55bdc1da22c84f3bcf0dff44567fb20159caad5b` with this follow-up:
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/3d84800e-3583-433d-a15f-04107bcced0e).
It executes 18 cases (7 effect controls, 6 staged controls, 5 legacy
differential controls). New controls compare full row/cell/disposition counts
to the exact summary, cover more than twenty rows and cells, verify default
non-export and dry-run behavior, and compare filtered versus unfiltered
non-JSONL output byte for byte after removing command provenance. Selected
streams match exactly, and filtered direct/staged runs reuse the same full
analysis artifact. Repeated filters cover both chunks without changing output.

A separate neutral public CLI run saved a 762-byte, one-row/one-cell stream
under `/tmp/debundle-cutover-20261101/tv-effects/jsonl-example/out/reports/tree/main/effect_conflicts.jsonl`.
Its uncovered disposition and pair/cell counts match the summary. The CLI
exits 1 for the fixture's existing constraining cycle after writing complete
rejection reports:
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/bff74aad-ebd2-4feb-8e13-aad8c9539878).
This validates streaming access, not narrower semantic coverage or a change
to either TV verdict. Full TV2 remains incomplete.
