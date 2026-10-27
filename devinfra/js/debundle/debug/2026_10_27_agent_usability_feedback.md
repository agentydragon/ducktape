# Debundle agent usability feedback

Collected 2026-10-27 from the integrator and three worker roles: structural
ownership, selector/policy authoring, and callback/effect analysis. This is an
experience report and prioritized request list, not a change to Debundle's
contract. Examples are generic; correctness fixes link to public synthetic
regressions. No private input source or diagnostic payload is included.

The gates earned trust by rejecting unsafe ownership changes. The largest
usability cost was discovering the next justified action after rejection. All
three workers independently highlighted causal diagnostics, graph provenance,
and contract selection.

## A — Explain the first causal barrier

**Priority:** P0. **Owner:** Debundle diagnostics. **Status:** open request.

A callback-factory rejection required temporary interpreter logging and repeated
builds to distinguish a bad signature, namespace mutation, argument exposure,
and prior callback taint. A resulting eager edge could have thousands of
neighbors. Its first cause was an earlier initializer, far from the proposed
module merge.

Provide an explanation that connects:

```text
requested ownership change
  -> constraining dependency
  -> callback promoted at initialization
  -> matched contract rejected
  -> first taint or failed guard, with its source witness
```

**Acceptance:** a supported CLI query returns the matched contract/signature,
first rejected guard, relevant source unit and span, and a directly executable
inspection command. A host-mutation rejection identifies the offending write.
Start with one causal path; allow expansion into all witnesses. This should work
without editing or rebuilding the analyzer. Preserve conservative fallback when
an obligation cannot be established.

## B — Make the graph/edit lifecycle explicit

**Priority:** P1. **Owner:** authoring CLI and adapter. **Status:** open request.

An existing graph becomes stale after a valid assignment change. Repeating a
refinement against the already-applied tree can correctly fail. An unrelated
module rename also invalidated a later batch's supplied graph. Recovery
required tracking which source, spec, binary and output tree each artifact
represented.

**Acceptance:** commands expose source/spec/engine provenance and identify the
specific stale input. The response distinguishes an unapplied preview, an
already-applied edit, and a graph that needs rebuilding, with the adapter's next
command. Support a reviewed batch over one exact source/spec pair and return
its resulting artifact state. A green current pipeline and a green proposed
ownership contraction must be labelled as separate proofs.

## C — Put a task chooser and contract guide before the reference material

**Priority:** P1. **Owner:** skills and documentation. **Status:** open request.

Small authoring tasks repeatedly led through skill references, CLI docs, SPEC,
design, purity and reproduction guidance. The README introduces an implementation
map before the getting-started workflow. The full correctness references remain
necessary for analyzer changes; source authors need a shorter route to their
operation and its obligations.

Use progressive disclosure:

1. Choose a task: inspect, regroup existing assignments, extract residuals,
   rename a file/binding, stabilize selectors, or diagnose a rejected gate.
2. Follow one complete, tested recipe, including the adapter's gate and writer.
3. Open the specific contract or diagnostic reference when needed.
4. Follow implementation links into the formal design when changing analysis.

Contract selection needs one compact decision table: the fact being asserted,
accepted input/result shapes, publication timing, remaining audit obligations,
and a runnable synthetic example. In particular:

- Purity, synchronous reentry, callback retention and later publication are
  distinct facts.
- A registry retaining callbacks across calls has a different lifetime contract
  from a factory returning them in a fresh result.
- A returned class instance does not satisfy a plain-object factory contract.
- Host assertions require the documented alias/intrinsic integrity premise.

**Acceptance:** an agent can select the applicable workflow and contract family
from the front door, then follow links to one normative definition of each
precondition. Skills should route readers to that definition instead of copying
safety-critical rules into several documents. Keep the rigorous detail available.

## D — Make inspection bounded, typed and machine-readable

**Priority:** P1. **Owner:** inspection CLI and report interface. **Status:** open request.

The structural corpus graph was approximately 690 MB. A single binding query
could return thousands of edges and neighbors; inspecting related wrappers
repeatedly loaded that graph. Runtime diagnostics also exposed callable keys,
byte spans, source-unit ordinals and parsed statement indices that needed manual
translation. A source unit and its raw statement index were different numbers.

Some failed `--format json` operations left an empty redirected JSON file and
reported the error as text on stderr. Successful selector validation with zero
reported issues also needed interpretation by the caller.

**Acceptance:** provide compact summaries, direction/reason filters, explicit
returned/total counts, and grouped binding/module-family queries. Preserve an
explicit exhaustive mode. Use typed references consistently and provide direct
crosswalks to source inspection. JSON mode should return structured success and
failure results with actionable reason codes while preserving nonzero failure
exit status. Validation should explicitly report success and checked/matched
counts. Reuse loaded indexes across related queries where practical.

## E — Make filename and identifier rules predictable

**Priority:** P1. **Owner:** authoring CLI. **Status:** open request.

For a camel-case owner (generic spelling: `displayModeSwitching`), `refine`
normalized the filename to lowercase, while `merge --target /absolute/path/displayModeSwitching.yaml`
preserved case. The explicit-file spelling was discovered during integration.
`module:` also looked like a plausible typed query prefix, but the relevant
identifier was `logical:`.

**Acceptance:** one documented, gated module-file rename operation preserves the
intended filename spelling, shows its normalized logical identity, and previews
the physical paths it will write/delete. Path/case behavior should agree across
authoring commands. Unsupported identifier prefixes should suggest the accepted
prefix and show a valid example before expensive graph work begins.

Purpose-based provider policy selectors were a useful improvement in
[#12233](https://github.com/agentydragon/ducktape/pull/12233). Surface that option
in the authoring guide: changing chunk identity can affect relative loader
behavior and should not be necessary just to address a policy.

Follow-up: [#12310](https://github.com/agentydragon/ducktape/pull/12310)
proposes `modules merge --batch` for exact-case file moves with one complete
pre-write gate, and repairs dropped source module notes. Its synthetic and
hosted checks pass. Consistent path behavior across the other authoring commands
and identifier-prefix guidance remain open.

## F — Support recovery of human module boundaries

**Priority:** P2. **Owner:** proposals, authoring CLI and architect workflow.
**Status:** open request; author judgment remains required.

Atomic extractability produced persistent numbered fragments whose boundaries
were poor source-file boundaries. Concrete recovery tasks included thirteen
fragments forming one stylesheet and twenty tiny wrappers around one shared
implementation. Other numbered fragments contained substantial classes with
sensible individual seams.

**Acceptance:** offer reviewed regrouping candidates with dependency evidence:
sole-use helpers, constants consumed by one export map, wrapper families, and
component/style ownership. Show the retained initialization seams and gate the
complete assignment. Treat numbered names as authoring debt. Support semantic
file renaming for existing good boundaries alongside consolidation.

Mutation output should remain readable. Selector coalescing in
[#12220](https://github.com/agentydragon/ducktape/pull/12220) addressed an observed
case of 29 identical templates being retained as separate records. Apply that
normalization through shared mutation paths while preserving metadata and
rejecting incompatible aliases.

## G — Generate a replay and proof manifest at the adapter boundary

**Priority:** P1. **Owner:** Debundle adapter and build tooling. **Status:** open request.

A diagnostic replay required a candidate binary, staged vendor overlay, dozens
of package-root arguments, source roots, configuration and graph provenance.
Handoffs reconstructed these from terminal history. Separate BBR problems
included omitted untracked inputs, stale printed invocation IDs, shorter-file
download tails and a source writer executing inside its downloaded runfiles
workspace.

These transport/runner failures belong to their build-tool owners. The Debundle
adapter can make the canonical route and evidence easier to verify.

**Acceptance:** produce a manifest with material input paths/digests, engine
revision, non-secret runtime/build settings, graph provenance, actual invocation
IDs and output locations. Label canonical validation and diagnostic replay
explicitly. Provide the replay command and an unambiguous writer destination.
Preflight the adapter's included inputs. Keep gate, regeneration equality,
runtime smoke and hosted CI results separately identifiable.

## Correctness lessons from fixes already landed

The requests above should make analysis failures easier to localize; conservative
rejection alone does not establish an engine defect. This work also found actual
bugs, now fixed and covered by public synthetic tests:

- [#12235](https://github.com/agentydragon/ducktape/pull/12235): optional calls
  lost callback effects or returned captures. Refining precision also needed to
  preserve namespace-publication fallback. Observable Node behavior and negative
  split controls were essential.
- [#12277](https://github.com/agentydragon/ducktape/pull/12277): an ordinary
  descendant write over-rejected an audited host-membership probe. Identity,
  prototype and visible back-alias negatives remain necessary.

Fixture failures should include CLI stderr, the expression/case under test and
available report paths. A missing expected conflict-report file should expose
the actual pipeline rejection that prevented it from being written.

## Suggested implementation order

Start with A, then B and G together, then C. D and E improve the individual
commands in those workflows. F improves the quality of the recovered code.

Build each improvement around an existing runnable workflow and a small
synthetic acceptance case, preserving the existing acceptance rules and the
exact source/spec scope.
