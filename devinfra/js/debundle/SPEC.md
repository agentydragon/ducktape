# Selector resolution: specification

What `debundle` guarantees when it places spec entities in a bundle chunk. How to
write selectors: <docs/selectors.md>. How it is implemented:
`docs/selector_resolution.md`.

## Resolved CLI invocations

The global `--print-invocation` option parses the selected command normally,
resolves its declared CLI values, environment-backed values, and meaningful
defaults, then prints a typed `ResolvedInvocation` and exits before dispatch.
It does not read graph contents, run source analysis or proposal work, create
operation outputs, write graph rejection reports, or modify spec files.
Resolution uses parsed CLI metadata, the current working directory, and
executable path metadata only; it does not require file inputs to exist or read
their contents. If `--invocation-out` is supplied, writing that requested
record file is the sole print-only write.

`--invocation-out <path>` persists the same record before the selected
operation starts. An execution record is provenance for the attempted call,
not proof of success or of completed validation. Its `requested_proof` records
the checks the command asks to run, and its `write_scope` records possible
operation writes. Neither field describes a completed result.

The record includes the current working directory, executable path, typed
argument values and their sources, replay argv, and a shell-quoted command.
Declared environment-backed values are emitted as explicit replay arguments;
explicit CLI values take precedence. The record does not dump ambient
environment variables. A replay preserves file selection and relative-path
resolution while the caller environment changes, but argv alone does not pin
the contents of a spec, source tree, graph, or executable. Reproducing a
mutation against a changed spec requires the corresponding input identity or
snapshot provenance.

Operation classification is based on the selected CLI shape: graph-snapshot or
diagnostic-snapshot read, source-snapshot read, fresh pipeline, snapshot
creation or verification, validated edit, unverified edit, conditional comment
operation, or read-only analysis. Unrecognized shapes remain unclassified.
Running an edit gate does not establish full pipeline, reconstruction, or
runtime validation.

## Read-only query failures

Once Clap accepts the command line, runtime failures from `describe`,
`inspect-source`, `graph-summary`, `atoms`, `coverage`, `scc`, and `cluster`
follow the selected output format. In `json` mode the CLI writes one object
with a typed `reason` and the full actionable error chain in `message`; in
`ndjson` mode it writes that object as one row. Both modes exit nonzero.
Both modes retain the same diagnostic on stderr. Reason codes are `io`,
`invalid_json`, `source_unit_not_found`, `unsupported_identifier_prefix`, and
`query_failed`. Unsupported-prefix failures include the identifier, prefix,
accepted forms, examples, and an available suggestion. Graph compatibility
failures retain the existing `graph_compatibility` rejection payload. In `text`
mode, runtime errors keep the existing stderr diagnostic and leave stdout
without an error report.
Clap parse and usage failures occur before this runtime boundary.

## Input and spec scope

One run or validation resolves one exact spec against one configured input
program. The program may contain multiple chunks; each entity is resolved only
against the chunk named by its spec tree. The matcher does not consult another
spec or another source snapshot. Structural matching follows the rules below:
tree equality with the documented holes and consistent lexical alpha-renaming.

Authoring tools may inspect that same spec and source, then report selector
diagnostics or propose structural candidates. Those reports do not establish
semantic intent and do not change the spec unless an author explicitly applies
an edit. A new upstream snapshot needs its own reviewed spec and exact validation
against that snapshot.

The `modules refine` and `modules merge` authoring edits may combine source-match
records with byte-identical templates and matching metadata into one ordered
capture list. Conflicting notes or overlapping local captures remain separate.
The combined document passes the same production selector resolution and edit
gate before writing; regrouping does not bypass source-unit identity checks.

`modules merge --batch` composes all requested whole-module merges before one
edit-gate check. Rejected plans and gate failures leave every source and target
unchanged. Successful batches retain exact physical filenames and all claim and
metadata fields, write every target before deleting sources, and use atomic
per-file replacement rather than a crash-atomic multi-file transaction.

## Accepted output

Unresolved claims prevent acceptance. A run may write diagnostic reports for a
rejected spec, but writes the reconstructed JS tree, browser harness, and vendor
assets only after final program validation. Final checks inspect the exact
reconstructed syntax, including synthesized imports and exports and vendor
replacement. Syntax placement must agree with the validated assignment, including
whole-statement moves and repeated declarations. Emission consumes an immutable
validated plan.

When browser output is configured, reconstruction also resolves the HTML module
entries and preloads against the retained chunks, prepares the bootstrap and
rewritten HTML, and enumerates copied assets. Invalid loading topology rejects
the plan before any accepted output is written, including during a dry run.
The browser writer materializes those decisions without rediscovering entries.

## Dependency-graph array ordering

The dependency graph preserves source order for content observations on array
literal identities that the chunk value-flow analysis can track, including
aliases and reads promoted from directly invoked chunk functions. Allocation
seeds a separate pending frontier for each tracked array origin, so later
opaque barriers remain connected even through unsupported protocols. Allocation
alone does not depend on preceding opaque operations or reset existing content
writers/readers: one source origin can represent multiple runtime arrays.
Before any recognized eager literal-origin array exists, opaque operations do
not start an array-content chain. Lazy literal-allocation origins are
conservatively live from the start because hoisted or escaped functions may
allocate before their lexical statement. This ordering also applies to pure readers:
call purity does not make an observed array's contents immutable. It is emitted
in both strict and dataflow-aware S-chain modes.

Array member writes, method calls such as `push`, escaped arrays, and calls
whose callable body has no direct graph summary remain opaque heap barriers.
The shared synchronous-call analysis also makes a source unit an opaque heap
barrier when it reports incomplete execution effects, regardless of a purity
hint. This covers observations through unsupported coercion or property-access
protocols, and unresolved synchronous calls, without duplicating the call
analyzer. Key enumeration (`for-in`), optional calls, and tagged templates are
also barriers when their array effects are not represented precisely. Barriers
constrain the preceding array-content frontier and later array observations.

Purity and binding-effect completeness do not prove heap locality or argument
nonretention. Native calls consuming array literals remain heap barriers once
tracked arrays are live. Checked implicit local construction has a separate
source-derived heap capability: admitted fields only initialize fresh instance
state, and native fields require an explicit fresh-allocation model under the
shared native identity/integrity guard. Only an exact certified `new` site omits
its own opaque heap effect and constructor-body promotion. Other calls or
constructors in the same statement, including another invocation of that callee,
keep their own effects. Callee and argument evaluation, binding initialization
order, and deferred method/callback effects remain independently constrained.
When native receiver refinements condition this heap certificate, the exact
construction source unit reports the consumed input references in
`heap_native_integrity_uses`. Source-only certificates have no such use, and
configured inputs that the proof did not consume are not reported. Empty uses
are omitted from JSON and default to empty when reading older reports.
This adds no author flag or purity exemption.
The census tracks `ArrayLit` source origins, not arbitrary arrays returned by
`Array()`, `Object.entries`, or foreign imports; it does not claim full heap
ordering coverage.

These are scheduling dependencies only; they do not certify call purity,
no-reentry, property-descriptor integrity, or prototype integrity.
`trusted_dataflow_summaries` does not weaken these barriers. Unrecognized or
ambiguous origins remain conservative rather than receiving a precise
per-array ordering claim.

## Authoring reports

Each per-chunk `modules.json` may include `imported_callables`, keyed by the
importer's local binding name. A `callback_factory_evidence` entry names its
defining module and export, with `origin: author_assertion` for an audited
contract or `origin: inferred_immutable_wrapper` for a checked wrapper. The
inferred entry also names the defining local binding and, when available, a
standard `source_location` (`source_path`, `start_line`, `end_line`,
`start_column`). This is a return-only callback capability, independent of
call purity. The field is omitted when no bare imported callable was resolved.

Source-certified immutable wrappers may also supply `callback_object_arguments`:
ordinary allocated argument positions and checked direct callback uses. An
anonymous inline consumer without self-identity access completes entry
coverage. Author timing assertions do not grant this capability. The
canonical native census uses it only for supported shallow conditional regions
on stable parameters in an anonymous inline callback or its immediate anonymous
IIFEs. Discharged member-write witnesses retain the original source site,
empty `affected_roots`, and `unreachable_branch` evidence naming the defining
wrapper, consuming import, wrapper call, callback and guard locations. Deferred
nested bodies and unsupported contexts keep their conservative witnesses.
This does not change purity, callback effects, exceptions or loader return
property reads.

Each `dependency_graph.json` written by `debundle run` carries input provenance:
SHA-256 of the executable engine and the exact bytes read for the tree config,
vendor marks, module YAMLs, optional binding patches, JS list, every listed JS
chunk, and vendor package files consulted by planning. Module YAML path
membership is recorded separately, including paths outside the graph chunk.
The provenance also retains a canonical runnable argv with resolved paths and
effective env-provided values, the original cwd, logical root paths, and the
config's chunk-to-module-tree mapping. A graph created before this metadata
existed remains a historical report only.

A command that combines a saved graph with current spec or source checks the
recorded baseline before reading claims or writing edits. It refuses changed,
added, deleted, or mismapped inputs and reports the mismatch with a regeneration
command that uses a fresh output directory. The recipe translates an output
root when a supplied current spec, source, or package root maps it. If no
supplied relocation root maps the output and a current spec root is supplied,
it chooses an unused `debundle-output-regenerated` directory under that spec
root, adding a numeric suffix on collision. Without a current spec root, an
unmapped output uses an unused `-regenerated` sibling beside the recorded
output, adding a numeric suffix on collision. An accepted edit
evaluates its candidate assignment against the saved dependency analysis; the
graph remains the **pre-edit** snapshot and the next graph-backed edit requires
another `debundle run`. A batch edit checks one
starting snapshot and one combined candidate. Graph-only historical views do
not certify current input compatibility.

`inspect-source --id unit:...` resolves that exact graph node without current
module claims. It skips the authored module-scope check, allowing flat-spec
graphs, while retaining captured-input, engine, and source identity checks.
Other graph-backed selections and edits require the authored scope.

Each graph chunk maps a logical `source_path` to the physical `input_file`
whose bytes were parsed. Selector resolution keeps the logical path as its
parser identity and uses source bytes retained by graph verification. A
logical alias may have no file at `--source-root/<source_path>`; the verified
chunk mapping still identifies its input. When `--source-root` is supplied, it
must contain the resolved physical input, and any existing logical-path
candidate beneath that root must resolve to the same file. Equal content at
another path does not satisfy this identity check. Relocate physical inputs
with `--graph-source-root`, which maps each captured source-root-relative path
under the new root. `--source-root` is only a selector lookup boundary and
does not change that physical mapping.

Description reports include `module_proposals` and `module_proposal_diagnostics`
only when proposal analysis runs. Their limits sections use the same field names.
Proposals name their outgoing residual quotient-class dependencies with
`edges_to_other_residual_classes` and `other_residual_classes_referenced`.

`cluster <binding>` emits at most 20 incoming and 20 outgoing module neighbors
by default. `--limit N` sets a positive cap for each direction; `--all` emits
every neighbor and sets `limits.limit` to `0`. The
`limits.sections.incoming_modules` and `outgoing_modules` entries record
`total`, `emitted`, and `truncated` after the selected direction and reasons
are applied. Text output names the same module paths and reports any truncated
section.

`describe` and `inspect-source --id` reject an unsupported identifier-like
prefix before graph compatibility checks or graph loading. Accepted typed
prefixes are `unit:`, `logical:`, `atomic:`, `diagnostic:`, `extend:`, and
`auto_partition_`. Existing module paths are checked before rejecting an
unknown colon prefix because colons are legal in module filenames.

`describe --batch` accepts a nonempty JSON array of identifiers and returns the
same per-selection reports as separate `describe` calls, in request order with
duplicates retained. One invocation loads and verifies one graph and shares
binding-home and requested proposal lookups. All selections use the same
roots, limits and filters; module source claims retain the ordinary resolver's
semantics. JSON wraps the reports in `results`; NDJSON emits one report per
item. A failed item emits only the command failure, with no partial reports.
The invocation record retains the batch-file path, not its contents.

## Delegated input snapshots

`snapshot create` copies one saved graph's verified input set and exact engine
bytes into a new worker directory. It preserves the input bytes and root
relationships, rechecks copied file digests, and publishes the directory only
after verifying the complete copy. Existing destinations are refused.

The snapshot includes source chunks, the complete captured spec/config input
set, dependency inputs, the graph, and a manifest describing the starting
state. Its returned regeneration argument vector names the worker's copied
engine, inputs and output directory. The returned `graph_package_roots` map
supplies each global `--graph-package-root NAME=DIR` override for graph-backed
queries and edits, including the `__packages_root__` fallback name. Separate
snapshots share no writable
input or output files. Snapshot verification continues to work after the
original workspace is removed or the snapshot directory is moved.
Browser source assets and their summary are copied and fingerprinted when
the snapshot is created.

`snapshot verify` checks the starting graph, engine, input contents and YAML
membership. After a worker edits its spec, this baseline check fails; running
the returned regeneration command produces analysis for the edited state.

Snapshot creation requires a graph produced by `run` from a tree-shaped spec.
Embedded source and vendor-bundle paths must be relative to the configured
source root, and all captured files must belong to declared relocatable roots.
Unsupported inputs are refused before a worker directory is published.
`describe` returns at most 20 rows per report section by default. `--limit N`
sets a positive per-section cap; `--all` returns every row and conflicts with
`--limit`. Each section has `total`, `emitted`, and `truncated` values under
`limits.sections`; `limits.limit` is the selected cap and is `0` for `--all`.
Filtering by `--direction` or repeated `--reason` flags happens before section
totals and limits are computed. Direction filters dependency, atomic-group, and
module-quotient edges. Reason values are the serialized `DepKind` names:
`eager_use`, `lazy_use`, `hoisted_use`, `eager_rebind`, `lazy_rebind`,
`deferred_rebind`, `sequenced`, `co_declaration`, and `local_effect`; repeated
values are combined with OR. Rows have stable ID ordering, with the selected
binding, source unit, atomic group, proposal, or diagnostic kept first in its
matching section before truncation.

## Unassigned source

Every configured chunk selects one `unassigned_mode` destination policy:

| `kind`             | Destination of unclaimed source                                                   |
| ------------------ | --------------------------------------------------------------------------------- |
| `inline_in_entry`  | Keep it in the chunk entry.                                                       |
| `catchall_file`    | Collect it in `target`, defaulting to `residual/unhandled`.                       |
| `per_atomic_group` | Emit one module at `__auto/atomic_groups/NNNN` per wholly unclaimed atomic group. |

An atomic group contains source units that must stay together. Explicit claims
are retained; selecting a destination policy does not relax the complete
assignment's realizability checks. The per-group policy decomposes unassigned
source deterministically and does not infer semantic module names.

## Entities

An **entity** is one thing a spec places in a chunk: a module member, a
`source_matches[].bindings[]` entry, or an `anonymous_statements[]` entry. Each
entity's selector names the places it may occupy: a top-level declaration or
import binding, or one top-level statement.

An entity is scoped to its module's chunk, and only that chunk's places are its
candidates. In a tree spec, each module tree names the chunk its modules are
scoped to; several trees may name the same chunk, and their modules then share
it as if authored in one tree.

`selector.binding.kind: import_specifier` names an imported local. Source checks
recognize named, default, and namespace imports. Logical-module member
re-exports currently materialize named imported locals only; member claims for
default and namespace imported locals remain unsupported during reconstruction.

## Repeated `var` declarations

Repeated declarations of the same module-scoped `var` denote one binding.
Selecting or renaming that binding applies to every declaration site, including
sites inside top-level control flow. A structural selector may identify it from
any of those sites. Its declarations must share one output module; initializers,
reads, and mutations retain their observable order. A bare redeclaration does
not reset the binding. Other duplicate declaration combinations remain unsupported.
SourceUnit-based relational selectors inspect facts on each statement site; they do
not union those facts across all redeclarations. Binding selectors canonicalize
to the first declaration site, so relations composed with them cannot inspect
later sites' facts. Control-flow statements containing `var` move intact; all
bindings they declare must be assigned to the same destination.

## Matching

Only the shape matcher decides where a `source_match` template matches. A
template matches a place when their syntax trees are equal up to:

- **holes** — `ANYTHING`, `EXPR`, `STMT`, `STMT_LIST`, `DECLARATORS`, `SEQ_EXPRS` and
  the other hole keywords of <docs/selectors.md> match any subtree of their kind;
- **alpha-renaming** — binding and value identifiers match any identifier,
  consistently within their lexical scope.

A **free** identifier, one the template uses but never declares, is not always
alpha-renamed. In order, it is:

1. a hole keyword;
2. a **reference** to the spec entity exported under that name: by the
   template's own module if it exports one, else by the one other module that
   does. A name several other modules export makes the template `invalid`;
3. an unshadowed **global** (`Object`, `window`, `console`, …) if the chunk
   neither declares nor imports that name at top level: it matches only itself;
4. otherwise alpha-renamed.

A place matches a reference only where the free name binds, throughout the
match, the one chunk identifier the referenced entity takes, whichever selector
pins that entity. A reference to an entity whose own selector fails before the
joint solve (`no_match`, `too_broad`, `invalid`) alpha-renames.

A `source_matches[]` binding may claim a free identifier of its template
instead of a declaration (**pinning by use site**). Its entity is the top-level
declaration that identifier binds to, throughout the match; a match where it
binds no top-level declaration, or different identifiers in different scopes,
is not a place of that entity. A claimed free identifier is the entity's own
binding, never a reference or a global. A claim of free identifiers only does
not claim the matched statement.

An anonymous statement written with a bare `match:` rather than `source_match:`
skips alpha-renaming: its identifiers must equal the chunk's. Literals,
operators, member property names, object keys and tree structure are
significant. Relational selectors (`cross_ref`, `reads_member`, …) match through
facts derived from the chunk, not through templates.

An `anonymous_statements[].source_match.match` may contain a contiguous run of
top-level statements. The final statement is the only claimed place; preceding
statements constrain the match through their shape and free identifiers and may
be owned by other entities. The context form requires an anonymous final
statement. A bare `anonymous_statements[].match` contains exactly one statement.
A context window does not use a top-level `STMT_LIST` hole.

## Local selector explanations

`inspect-source` labels the parsed file's top-level statements and original
locations. `spec match-selector --explain --statements` compares one inline or
authored selector against the entire selected range using its structural
matching semantics. It reports a local match, mismatch, unsupported comparison,
or diagnostic limit. An inconclusive comparison is not a mismatch.

This operation does not establish uniqueness, enforce ownership/all-different,
or resolve references to other spec exports. Loading a selector from a spec
preserves its matching mode and claimed bindings without solving other entries.
Local match results are distinct from resolved assignment outcomes.

## Assignment

Every chunk's entities form one program. Each entity takes exactly one of its
matched places, jointly with every other entity:

- no two entities claim the same place, whichever modules or trees they come
  from;
- a relational selector holds between the places its entities take;
- a template's references agree with the places their entities take.

The answer is the **unique** assignment satisfying these. An assignment that
holds only for some solutions of the joint problem is not an answer.

## Outcomes

Every entity gets exactly one outcome, in one record format shared by `run`,
`spec validate` and `spec match-selector`:

| Outcome                                   | Meaning                                                                                                                                                                                    | Severity |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------- |
| `resolved`, `resolved_by: own_selector`   | its selector alone matches one place                                                                                                                                                       | ok       |
| `resolved`, `resolved_by: own_references` | several places match its selector; only one agrees with the places of the `references` it names                                                                                            | ok       |
| `resolved`, `resolved_by: elimination`    | unique only because the named `claimers` took its other places                                                                                                                             | warning  |
| `resolved`, `resolved_by: referenced_by`  | its selector matches several places; the templates of the named `referrers`, which name it, pick one                                                                                       | warning  |
| `no_match`                                | no place satisfies its selector and constraints; a one-statement template may list up to 3 `nearest_unclaimed` statements (no entity claims them), closest first, with where each diverges | error    |
| `unsatisfiable`                           | its interacting group admits no joint assignment; optional `witness` proves an ownership conflict among a subset                                                                           | error    |
| `ambiguous`                               | several assignments exist; at most 5 of its places are listed, with `truncated` when more exist, else with `differentiators` (below)                                                       | error    |
| `conflict`                                | none of the places its selector matches agrees with where the entities its template references (`with`) resolve                                                                            | error    |
| `too_broad`                               | its selector matches more than 100 places, and it takes no further part                                                                                                                    | error    |
| `duplicate_claim`                         | resolved to a binding another entity already claims; `declaration` gives the original body index (`body_index`) and its keyword `kind` (`function`, `const`, `import`, ...)                | error    |
| `invalid`                                 | its selector does not parse, uses an unsupported construct, or its matches do not map to places                                                                                            | error    |
| `undecided`                               | the solver stopped before deciding it                                                                                                                                                      | error    |

An `ambiguous` outcome's `differentiators` give each listed place that has one
a stable anchor the other listed places lack: a literal, object key, class
member, member-path call, declaration kind or arity, in the place's own
statement or else in the statement adjacent to it. A place with none is not
listed there, and the report says when no place has one.

A contradiction affects only its group of interacting entities. Every member
reports `unsatisfiable`, with `group` naming the group's first entity as a
chunk-local identifier. An optional `witness` names distinct selector claims
and their complete candidate-source-unit union, with fewer source units than claims.
This proves an ownership conflict but need not be minimal. Each witness row records
the post-split `source_unit` ordinal, original `body_index`, and matched bindings.
Witness members participate in that conflict; other group members remain
blocked. Without a witness, the group remains infeasible and the diagnostic
states that no smaller ownership witness was found within diagnostic limits.
Independent groups resolve normally.

`run` and `spec validate` in both modes give each entity the same outcome. The
edit gate and `describe` resolve only `source_matches[]` entries and anonymous
statements, without the spec's other members, and give those entities the same
outcome except that one unique only because a relational member claimed its
other places is `ambiguous` to them, and a template reference to any other
member alpha-renames there. Only the pipeline
(`run`, `spec validate --spec`) reports `duplicate_claim`. `spec match-selector`
resolves its selector as a spec of one entity, so a selector resolved by
elimination or by its references in a spec is `ambiguous` there.

`spec validate` also reports selector-validation evidence independently of its
issue rows. `checked` counts one member, one expanded
`source_matches[].bindings[]` entry, or one anonymous statement that was
evaluated; parse or resolution failures count as checked but not matched.
`matched` counts checked entities whose final outcome is `resolved`, including
those resolved by elimination or references. A module-level diagnostic such as
a stale annotation can fail validation without adding a selector to these
counts. `success` means there are no error-severity selector outcomes and every
checked selector matched. It describes selector validation only; it does not
claim that the assignment gate, emission, or runtime checks passed. An empty
selection reports success with `checked: 0` and `matched: 0`, making clear that
no selectors were evaluated. Outcome rows remain diagnostics, not the source of
these totals. If the full pipeline stops before all configured chunks report,
`spec validate` preserves that error instead of returning partial evidence.

## Modes

- **Keep-going** (the default): every outcome is reported, entities with an
  error outcome stay unclaimed, and a chunk with any error outcome fails once
  all of its outcomes are reported. Every chunk is reported; the run fails
  with the first failing chunk in chunk-id order.
- **Fail-fast** (`--fail-fast`): the first error outcome stops the run. Outcomes
  come in a fixed order: every chunk's duplicate claims, then every chunk's
  resolved outcomes, chunks in chunk-id order.

Warnings never stop a run.

## Read-only decomposition candidates

Opt-in atomic-conflict coarsening joins whole candidate groups connected by
native reported structural atomic groups. It preserves the residual entry path
when a joined group contains that entry, then validates the complete assignment
through the shared gate before blocking-SCC coarsening. Without the opt-in,
structural conflicts stop coarsening and remain reported.

Opt-in original-home rejoining starts only from a complete atomic/SCC candidate
accepted by the shared realizability gate. It contracts whole candidate groups
only when every source unit in both groups has the same original module home; an
existing mixed-home coarsened group is never split or joined to a pure group.
Each proposed contraction passes the full shared realizability gate on its
hypothetical complete assignment, and the final assignment passes that gate again.
The command reports a proposal and does not edit the spec. An optional per-home
pair-attempt limit bounds partial fallback checks across all passes while
preserving whole-home checks; the report identifies homes left with untried
pairs. An optional whole-home pass limit bounds complete stable-order passes
across original homes; zero skips rejoining and one permits at most one pass.
If the limit stops a productive pass while pure homes remain split, the report
says `whole_home_pass_limit_reached` and includes the actual
`whole_home_pass_count`. The default remains exhaustive;
limits never bypass either gate check.

With `--preserve-selector-anchors --source-root`, decomposition resolves
authored selectors against their source chunks through the joint selector
resolver. It reports local `intrinsic_alias` and module-neutral ambiguous
source-match references in `selector_anchor_relations`, including named-member
and anonymous-statement sources. Each relation carries a tagged `source`, the
referenced member, and both source-unit-ID sets. Anonymous sources use their authored
`anonymous_statements[]` index; the resolver maps that selector to its stable
source unit before coalescing the candidate groups. A module-neutral unique reference
does not force co-location. The option is read-only and the complete candidate
still passes the shared structural and realizability gates.

## Readable names and the chunk public surface

`name:` on a member and the name assigned to `source_matches[].bindings[]`
name the spec-owned binding in its logical module. If the source chunk already
exports that binding, emitted output keeps the original chunk export name and
adds the spec name as a second export of the same binding. The two ESM export
names are live aliases; existing consumers therefore remain compatible.

Named imports in other processed chunks may import the readable alias and bind
it locally under that readable name. This import-local change is conservative:
a collision with an existing or nested binding, an ambiguous alias, or an
unprocessed importer leaves the old import form unchanged. Dynamic imports and
namespace imports continue to use the original minified export names. Bindings
without a spec-assigned name are not changed by this cross-chunk pass.

## Runtime loading contract

Flat and tree specs accept `execution_runtime: generic` (the default) or
`execution_runtime: native_browser_esm`. The latter declares execution through
the browser's native ESM loader, with no synchronous host API that can obtain
the namespace of a module while that module is evaluating. It is independent
of browser-harness output configuration.

The native-browser proof still checks a complete ordinary-ESM static closure,
absence of top-level await and absence of a cycle back to the selected chunk.
CommonJS compatibility references alone do not expose that chunk's exports
through the native loader. Callbacks actually passed or published remain
tracked, including callbacks that escape through global or unknown objects.

Vendor replacement may preserve the proof when the original residual edges
over-approximate the effective dependencies. A bundled partial replacement
qualifies only when its prepared bundle is an ESM leaf with no static requests
or top-level await: generated facades lead only to that leaf, while residual
pruning can delete original edges. Bundles with external requests, raw package
swaps and full package swaps retain conservative export reentry. Boundary
renames and suppressed chunks add no dependency edges.

## Hoisted function imports

A direct eager read of a function declaration retains a `hoisted_use` source unit
edge. The function binding is initialized during ESM instantiation, so this
edge does not constrain evaluation order or atomic co-location. It still
participates in the imports graph: entering the function's module can change
the traversal order and expose a TDZ on another binding. The gate must model
that import even when reading the function itself is safe.

## Export-reentry diagnostics

Materialization reports the selected export-reentry policy and its observed
blockers in `export_reentry.json` beside the per-chunk dependency graph. This is
diagnostic evidence only: it does not change the analyzer's proof or emitted
JavaScript. `Full` reports include accepted and rejected chunks; rejection-only
reports include it on rejection, while accepted dry runs remain silent. The
typed wire schema and blocker fields are described in
<docs/wire_format.md#export-reentry-diagnostics>.

## Partial vendor-swap ownership

The vendor strip pass assigns top-level items to swapped packages through
source reads and rebinds (including nested function bodies), plus writes to
referenced local values. Inferred possible call targets do not assign package
ownership: a function passed to a different invocation of the same helper is
not thereby part of this package. The strip pass rejects non-shareable items
also needed by residual exports and checks retained bindings and observable
writes before removing swapped items.

## Synchronous analysis limits

Recursion and analysis limits preserve the first-await boundary where retained
callback read coverage is complete. Possible synchronous reads and writes remain
constraints, including for private callbacks that have not escaped. Unsupported
bodies and dynamic scope retain conservative whole-body effects. Opaque-call
reachability follows those same synchronous prefixes, so a value first read
after a definite `await` does not expose its callback body through that path
during chunk initialization.

Ordinary eager reads and synchronous callback reads stay between the known
preceding and following writes of the binding, including `var` initializers
and later assignments, even when the eager reader's source unit is pure.

Opaque calls holding a class or instance may invoke its constructor, field
initializers, methods, and accessors. Their synchronous read closure stops at a
definite `await` where retained callable coverage is complete; uncovered source units
and incomplete reachability retain whole-body effects and rebinds.
Defining a pure, unpublished subclass with a direct, uniquely known ordinary
class superclass reads that superclass for the eager dependency graph, but does
not by itself publish or invoke the superclass constructor or methods. Actual
construction and publication remain reachability roots; unknown or alternate
superclass values retain conservative roots.
When those effects reach a top-level class declaration with a simple binding
as its superclass, analysis uses the possible class values from the prefix at
the declaration. Later writes to that binding do not replace the superclass
captured by the class. Unsupported or uncertain heritage retains the generic
fallback.

A pure initializer that calls a uniquely known local function can retain an
unpublished callback graph in a fresh ordinary record without making that
initializer a speculative reachability root. The function must have simple
parameters, no synchronous calls, writes, dynamic scope, defaults, or other
effects, and a single return value modeled as a plain data record. Its values
may be parameters, local or inline functions, primitives, or nested records
of the same shape. Arguments must be inline functions or primitives. The call
and its eager dependencies still run normally; any later export, write, or
invocation of the result restores its callback reachability.

A second source-proven record form permits ordered assignments to distinct
pre-existing own data fields of one fresh local object. The function body must
contain exactly a `const` initialized by a plain object literal with static
data keys and literal values, one or more simple static field assignments from
simple parameters or literals, and a return of that same local. Every written
field must exist in the initializer and be written once. This avoids inherited
setters, accessors, proxies, aliases, reassignments, callback calls, external
publication, and write-order uncertainty. A shared certificate is consumed by
function-body purity and synchronous callback analysis; the latter also checks
that the execution index fully represents the writes. If any check fails, the
ordinary effect and reachability paths apply. Calls and eager dependencies are
still evaluated; later use or publication of the result can promote its
retained callbacks.

A direct local wrapper call may also retain callbacks in a fresh checked class
instance without publishing them. The wrapper must have one return containing
only checked local `new` allocations, simple parameters, no other calls or
effects, and a unique stable identity. Inputs must be fresh plain-data records,
inline functions, or primitives. A fresh record may contain a dense fresh array
of these inputs or initialized, exact own primitive members of a stable,
single-initializer TS enum object. Enum reads retain their eager dependencies;
mutation, escape, holes, spreads, and unknown keys retain conservative
reachability. Nested allocations must be inert; constructor
property reads must resolve to own data or safe missing properties under A11.
The initializer, wrapper, and class source units still contribute actual synchronous
effects and ordering. Their execution alone does not expose deferred methods or
make them available as host accessors. Later invocation or publication of the
result restores those capabilities. Unsupported constructor paths retain
conservative reachability.

A binding annotation `retained_record_factory: true` supplies only audited
prototype premises for this source-checked local factory proof. The author
asserts that missing input fields read by the checked constructors are actually
absent along their prototype chains, and that instance assignments successfully
create ordinary own data properties without interception. Getter-freedom alone
is insufficient: an inherited data value can retain an unrepresented callback.
This assertion can override conservative prototype-mutation census blockers;
it does not declare the factory pure or native identities intact.

Admission requires a direct unary call of the selected stable top-level hoisted
factory with a fresh literal data record. The canonical AST check excludes
assignment/sequence-wrapped records, spreads, computed keys, accessors,
`__proto__`, and other eager calls. Nested dense arrays, literal values, inline
callbacks and independently checked enum reads are supported. The ordinary
source-checked return-only constructor proof and normal execution remain in
force, including eager lexical reads, TDZ, argument evaluation, exposure checks,
side effects and ordering. Only the exact checked constructor/property and
input-object/missing-property pairs consume the assertion. Failed source proof
or runtime admission restores conservative reachability and clears those pairs.
Consumed author obligations appear in `synchronous_execution.local_record_factory_uses`,
separately from inferred native integrity.

An implicit local class constructor can also receive a source-derived
construction certificate. The class must be a direct declaration in the same
chunk, without inheritance, decorators, an explicit constructor, computed
keys, static fields, or static blocks. Instance field initializers may be
absent, primitive literals, boolean negation of a numeric/boolean literal,
deferred function/arrow allocations, or zero-argument construction of an
unshadowed built-in `Set`, `Map`, `WeakSet`, or `WeakMap`. Ordinary method and
getter bodies remain deferred. Purity admits only a direct zero-argument
top-level `new` initializer after that declaration when whole-chunk value flow
resolves its binding to exactly that class. It suppresses only construction's
side-effect ordering; method invocation, callback escape, and ordinary eager
reads keep their own checks. The synchronous interpreter consumes the same
class certificate and retains its separate binding-identity and deferred-body
checks. Unsupported shapes and uncertain identities keep the conservative
paths. Before any semantic consumer uses this composed certificate, its native
field identities must pass the canonical execution index's source-derived
integrity guard. Purity and synchronous execution share that validated result;
recognized native replacement invalidates both. This uses the existing local
and whole-program native census and does not change the catalogue's A11
assumption for other whitelist admissions.

When a Set or local-class purity certificate consumes a captured value input,
the resulting pure verdict carries the exact `value_inputs` references in its
`dependencies` field. Those references are unioned through pure local calls,
binding initializers, and inferred cross-module exports, including fixpoint
rounds where the verdict stays pure but learns another dependency. Empty
dependency sets are omitted, preserving the existing `{ "kind": "pure" }`
report shape. An author `pure_exports` assertion remains its own axiom and
does not acquire captured-value references from unrelated inference. The
dependency field records the assumption used by that purity decision. The
captured-object assertion itself does not declare the capture initializer pure,
prevent callbacks or throws, or establish freshness or initialization order.

At-init callback analysis also records possible implicit primitive conversions
from arithmetic, relational and loose-equality operators, the left operand of
`in`, numeric unary operators, updates and non-logical compound assignments.
If retained value flow proves an operand primitive, the conversion adds no
callback effect. An object or unknown operand uses the conservative synchronous
callback path, covering custom `[Symbol.toPrimitive]`, `valueOf` and `toString`
methods without assuming which conversion method runs. Strict equality,
short-circuit operators, `typeof`, logical negation and `instanceof` do not
create a ToPrimitive effect. Callback effects reachable through an operand's
retained lazy closure are included as well, covering an accessor that returns
another conversion callable. This is callback-effect analysis; it does not
classify the enclosing operator as pure.

Native call models share one checked projection for call timing and successful
return values. A direct `Math.round`, `floor`, `ceil`, `trunc`, `abs`, or `sign`
call with one proven primitive argument has no synchronous callback effect and
returns a primitive. That return fact composes with subsequent conversions.
Direct `Symbol` and `Symbol.for` calls with zero or one primitive argument also
have no synchronous callback effect and return a primitive.
Direct `RegExp` calls and construction admit zero to two primitive arguments
for timing only; their results remain opaque and compilation may still throw.
Fresh-record `Object.freeze` uses the same projection and returns its original
argument identity, preserving callbacks stored in that object.

The intrinsic must be undeclared and unshadowed. Dynamic scope or visible
writes to its intrinsic/global family, including aliases and reflective writes,
disable the model. Spreads and unsupported input shapes remain conservative.
Callee and argument evaluation retain their effects and throws. These models
neither classify calls as pure nor bypass their ordinary source-order constraints.

## Purity hint diagnostics

The per-chunk `modules.json.redundant_purity_hints` report disables each tested
`purity: pure` hint before checking redundancy. Its `proof_basis` distinguishes
`without_purity_hints` from `with_other_purity_hints`; the latter is conditional
on retaining other chunk-local purity hints. These diagnostics do not change
acceptance or edit the spec. Remove hints individually and revalidate. A missing
entry is inconclusive. Probe scope: <docs/purity_soundness.md#redundant-purity-hints>.

## Chunk-export contract audit

`debundle spec audit-contracts` loads the current flat or tree spec and source,
requires one exact `chunk_export_purity` policy key and a positive finite
`--max-probes`, and runs the ordinary full dry-run acceptance pipeline. A
failing baseline stops the audit before any removal verdict. Each selected
contract is then removed alone from a fresh in-memory copy of the same
authored spec and the full pipeline is recomputed, including vendor resolution,
assignment, lowering, and final reconstruction validation. No saved graph is
accepted as proof. The command writes no authored spec, emitted output, or
probe graph. It compares complete captured input provenance between baseline
and probes and verifies those files again before reporting. The JSON report
includes `input_provenance_sha256` for the exact captured input set and engine.

The target identity is typed: policy key, contract kind, export, optional
member, and optional callback signature. A `source_markers` policy key remains
a policy label, never a fabricated chunk ID. Filters `--kind`, `--export`,
`--member`, and `--signature` select exact targets; signatures use the typed
JSON object with `argument_count` and `callback_arguments`. Every selected
target beyond the budget is reported as `budget_skipped`.

`removable_under_remaining_assumptions` means only that the configured source,
spec, and assignment still pass full acceptance after this one removal while
all other author contracts and runtime assumptions remain. It does not prove
the removed contract independently inferred or pure. Individually removable
contracts are not necessarily jointly removable. `failed_full_acceptance`
means the changed candidate failed the shared pipeline; it does not establish
that the contract is semantically true or uniquely necessary. A failure before
input capture is `inconclusive`. The audit does not apply removals.

Optional `--checkpoint` creates a new record; `--resume-from` loads and updates
one. Both modes hold an exclusive kernel lock on a stable sidecar. Resume always
runs fresh full baseline acceptance, then compares complete `GraphProvenance`
and normalized selection before reusing definitive target results. Inconclusive
results are retried; budget skips are never stored. `--max-probes` remains the
total deterministic prefix, including reused results; a smaller cap retains
saved results outside the reported prefix. Verified definitive results are
saved by atomic sibling-file replacement, with no power-loss durability claim.
The final report schema and independent-removal semantics remain unchanged.

## Source-anchored export policies

`chunk_export_purity` normally keys contracts by the defining chunk ID.
An optional `source_markers` list instead identifies that provider by a
conjunction of purpose-bearing substrings; the key then serves only as a
policy label:

```yaml
chunk_export_purity:
  lazyRegistry:
    source_markers: [neutral-lazy-registry, private-callback-storage]
    pure_exports: [factory]
    inline_callback_factory_exports: [factory]
```

Matching uses the loaded provider entry's current analysis source before
logical-module lowering: exact input text for pass-through source entries,
or the current staged AST's rendering for retained AST entries. It does not
read filenames or original text from an earlier parser snapshot. Every marker
must be nonempty, and the conjunction must identify exactly one loaded
provider, including providers outside the selected chunks' import closure.
Zero/multiple matches, an empty list, whitespace-only markers, and multiple
policies resolving to one provider are spec errors. An explicit chunk-ID
policy and a source policy for the same provider also conflict.

Only the policy map is resolved to the existing provider ID. This does not
alias, select, rename, split, relocate or rewrite the provider, and it grants
no admission override. It can therefore configure an analyzed but unselected
provider whose `import.meta` context must remain in its existing output file.
Existing input/output relocation rules remain unchanged. The markers identify
a provider; they do not prove its contracts. Each purity, callback, intrinsic
and publication premise still requires the same independent source audit.

## Inferred native property-key domains

Native mutation analysis distinguishes finite string keys, definitely Symbol
keys and unknown keys. Stable aliases of unshadowed `Symbol.for(literal)` and
lexical `[key, value]` loops over closed data-object `Object.entries` inputs
can preserve unrelated named global bindings. The latter requires intact
Object operations and Array iterators for both the result and pair
destructuring. Mutation directly on a native object remains a mutation of that
family, regardless of key domain.

A11 supplies initial native identity. All analyzed chunks contribute their
canonical mutation facts, including deferred writes, before resolving a closed
write invariant and propagating invalidated operation dependencies to a fixed
point. Providers and consumers share this inferred integrity input; it is not
a spec assumption or configurable override. Unknown keys, unstable bindings,
shadowing, unsupported patterns and visible native replacement fall back
conservatively. Global-slot value reads retain unresolved native identity
through aliases and descendants; only proved global-object receivers receive
key disjointness. A11 remains an author obligation for hidden native-alias
paths. Key precision grants no call, purity, throw or return summary.

## Inferred local receiver identity

The canonical execution index can discharge a direct member mutation's native
alias roots when an unconditional local initializer dominates that receiver
read. It retains source-positioned definitions, applies overwrite kills, and
rejects reaching assignments or rebinding from another activation. Receiver parameter cells,
loops, exceptional transfers, labels, unindexed object-literal accessors,
async/generator bodies and dynamic scope retain the existing census fallback. Destructuring and reflective mutation
calls are outside this initial proof's evaluation-order boundary.

Supported initializer results are fresh object literals, direct construction of
a stable ordinary function declaration with no explicit returns, and a stable
ordinary wrapper whose sole statement returns that construction. Every joined
alternative must qualify. Identity is independent of constructor purity: argument
effects, helper calls, throws and ordinary property effects remain represented.
The proof never propagates clean identity through the allocation's members or
through a global-slot read-back.

Graph `native_receiver_certificates` records each discharged site, its prior
mutation roots, initializer and constructor/wrapper source provenance. `describe`
selects local certificates or those relevant to evaluated failed native roots,
with the usual per-section row limits. Other native writes still contribute to
the unchanged whole-program integrity fixed point. No author contract is added.

## Captured ordinary-object values

`chunk_analysis_options.<defining chunk>.value_inputs` can attach a typed
`ordinary_object` value premise to one provider binding:

```yaml
chunk_analysis_options:
  static/peer:
    value_inputs:
      - source_match:
          match: "var captured = globalThis.runtimeSettings.state;"
          bindings: [captured]
        value:
          kind: ordinary_object
        audited_source_sha256: "<64 hex characters for the parser input>"
```

`source_match` must resolve through the canonical selector engine to exactly
one declared placeholder binding. `audited_source_sha256` must match the exact
bytes recorded as the provider's parser input, including when the graph was
freshly recaptured. `expected_export`, when present, must name a direct export
of that exact local binding. The provider must be loaded, non-excluded and in
the existing analyzed static-import closure. It may remain unmaterialized; this
option does not select it as an emission root or change output ownership.

The author audits that the successful initializer's direct value is an ordinary
non-Proxy object whose identity differs from the actual global object and every
native constructor, prototype or object identity represented by the census.
This says nothing about purity, reentry, throws, allocation freshness,
properties, descendant identities or object lifetime. The object may have a
native prototype, contain native values or expose getters and callbacks. Its
initializer and all reads, effects, callback calls, throws and ordering remain
in the ordinary analysis.

The current source-derived boundary accepts only a top-level simple binding
initialized by an opaque binding-rooted member read. The canonical declaration
and write index must establish one declaration and no possible reassignment,
update, repeated declaration, destructuring write, loop write, deferred write
or dynamic-scope contamination. A recognized global or native identity
contradicts the contract and fails; unsupported initializer categories are
rejected rather than interpreted as values. Stable aliases are propagated only
when the existing index proves their availability and write stability.

A mutation through the direct captured value discharges only that receiver's
prior `unknown_global_value` possibility. The census still emits an author-
conditioned witness, including when its affected-root set is empty. A member or
descendant of the captured object remains `unknown_global_value`, including
computed, symbol and `constructor` paths. Calls or construction involving a
captured value are widened only when the existing value facts show that input
is involved. A synchronous local callable whose canonical return summary
contains only the selected binding is recognized as returning an opaque
descendant, so its call result keeps the `unknown_global_value` barrier. A
synchronous callable whose returns are all statically primitive is also
accepted; those results do not create captured-object aliases. Mixed, unknown
or member-valued returns, and captured async or generator callables, are
rejected as unsupported. This remains an intra-chunk direct-return case, not a
general return or cross-module alias analysis. A write through a descendant
therefore retains the usual global/native barrier, and recognized native writes
elsewhere in the analyzed program remain effective.

The census records each input reference actually consumed by a receiver
refinement. Those dependencies flow into successful native guards, native-call
identity uses, inferred imported returned-record certificates and uses, and
the separate timing, purity and heap results for Set or local-class proofs.
Heap reports associate each use with the source unit where the certified
construction removes its opaque heap barrier. A source-only local-class proof
carries no value-input use. The source digest
binds the input to parsed bytes, but it does not establish the external runtime
audit, code outside the analyzed prefix, or host behavior.

## Ordinary own descriptor inspection

A guarded `Object.getOwnPropertyDescriptors` call can capture exact ordinary
object-literal descriptors through the object's canonical identity, including
a local binding or alias. The source must be untainted, and its current own-key
set must still equal the indexed static shape. Statically known string and
number keys are supported, including literal-computed data keys; computed
method/accessor keys, spreads, decorators, `__proto__`, unknown/added keys,
escaped or unsupported mutations, and native replacement remain conservative.
Callee/argument effects and throws remain represented; inspection captures
data-value identities and getter, setter and method callbacks without invoking
accessors.

The descriptor-map carrier has no general readable shape. The only modeled
composition is a direct, guarded `Object.defineProperties` call using that live
carrier and an untainted, currently empty fresh ordinary target. Captured data
values and accessor callbacks are applied to the target's existing identity,
so aliases observe the update. A repeated capture callsite with conflicting
source identity or descriptor values is invalidated rather than joined. Map
member access/mutation, return, escape or opaque consumption invalidates
application but retains the callback graph. A copied accessor read promotes
its getter; opaque target use retains getter, setter and method callbacks.
With the exact unmodified native identity and intact Object prototype guards,
the result expression may retain the first argument's target identity even
when application eligibility is not proved; it also retains a possible throw.
This result-shape fact does not authorize descriptor application or suppress
the opaque timing/effect fallback.
Returned/readable maps, arbitrary descriptor edits, `Object.assign`, imported
wrapper bodies, general purity and exception-free behavior remain outside the
contract.

## Inferred FinalizationRegistry callback retention

A direct `new FinalizationRegistry(cleanup)` or
`new globalThis.FinalizationRegistry(cleanup)` may use the native call model
when `cleanup` evaluates to exactly one known ordinary function origin or
closure. The constructor retains that callback in its new registry; it does
not invoke it during construction. The successful result is opaque and retains
the callback and its captures. Later use or opaque consumption promotes those
callbacks through the ordinary retained-value analysis.

The bare global and `globalThis` binding must be undeclared and unshadowed.
`globalThis` support is limited to direct static dot-member callees, including
existing modeled calls such as `new globalThis.Map()` and
`globalThis.Object.freeze(record)`. Local aliases, computed or optional
namespace access, spreads, unknown or mixed callback values, and noncallable
inputs remain on the conservative path. Dynamic scope, or any visible write to
the global object or the target intrinsic in any analyzed module, disables the
model. Callee and argument evaluation, throws, and at-init ordering remain
represented. The constructor remains impure and opaque for heap ordering; this
is not a purity, throw-freedom, fresh-allocation, registration or method model.

The [FinalizationRegistry constructor](https://tc39.es/ecma262/2025/multipage/managing-memory.html#sec-finalization-registry-constructor)
checks callability, creates registry state and stores the callback through
[`HostMakeJobCallback`](https://tc39.es/ecma262/2025/multipage/executable-code-and-execution-contexts.html#sec-hostmakejobcallback).
The default non-browser host operation records the callback for later jobs.
The [HTML host operation](https://html.spec.whatwg.org/multipage/webappapis.html#hostmakejobcallback)
also records the callback and execution context without invoking it.

## Inferred returned-record construction timing

For a stable directly exported hoisted zero-argument function declaration that allocates one fresh
empty Map and returns only noncomputed methods/getters, source analysis may
certify that construction cannot synchronously reenter importer code. This is
independent of purity and possible throws. The proof requires an exact defining
entry/export and the shared intrinsic-integrity checks across analyzed modules.
The returned record retains opaque deferred capabilities: later access,
invocation and opaque consumption conservatively preserve importer live-cell
reads and callback effects. The summary and provider member/capture provenance
appear in `modules.json` under `imported_callables.*.returned_record`.
Used certificates also appear in the dependency graph's
`imported_callable_summaries.*.returned_record`. A source unit's
`synchronous_execution.returned_record_uses` separately records `call_timing`
and `return_capability` consumption with the call-site location and a summary
reference. Callback-input factory evidence and uses retain their own fields.

## Audited callback factories

Optional calls retain conservative callee, receiver and argument callback
effects even when their value and short-circuit behavior cannot be inferred.
Known local return graphs keep their callable captures and a possible undefined
result. Optional calls do not acquire assertions scoped to ordinary direct-call
syntax.

Without an author assertion, a direct named import may receive an inferred
callback-factory summary from its defining entry file. The export must have one
stable identity: a hoisted function declaration or a definite top-level
function initializer. Its synchronous body only returns a fresh ordinary
zero-argument function. The admitted importer call supplies exactly one inline
callback; any other ordinary identifier parameters are omitted. The returned
function may capture only those parameters and its own locals; indexed calls,
writes and property reads must not introduce an additional callback capability.
The summary defers the supplied callback after a successful factory return,
while preserving eager import order, possible pre-initialization throws, and
the factory call's ordinary effects and purity classification. Other call
shapes and unproven source behavior use conservative timing.

`chunk_export_purity.<defining chunk>.callback_factory_exports` lists named
exports whose calls are audited to defer a single callback argument.
`callback_factory_members` lists static members on an imported namespace with
the same contract. At an importer, the analyzer applies either contract only
to a call with exactly one non-spread inline function or arrow callback, or a
retained result of another audited factory. The retained input is a fresh
ordinary callable/plain-object graph, and the outer factory must preserve the
original callback inside its own fresh returned graph. Construction and other
argument shapes remain conservative. The provider must not invoke that callback or any
caller-, host-, or export-provided callback synchronously, or publish the
callback outside the returned fresh ordinary callable/plain-object graph. The
consumer may later reach or invoke the captured callback through that result.
An invocation includes synchronous reads by the callback and by callbacks it
returns synchronously. Reads after an unconditional `await` remain deferred;
unsupported callback bodies retain conservative whole-body effects.
The provider must not synchronously read or write importer-owned state through
another route, including imported lexical cells or caller-owned objects; effects
confined to provider-private state are allowed.

The author must audit every provider and type branch, helper, callback-bearing
property access on both input and result, and prototype integrity; neither
graph may contain proxies or accessors at the call. This assertion describes
callback flow only. It does not
assert that the factory call is pure or effect-free. Writes through the result
that can install opaque values, accessors, or a new prototype conservatively
restore callback effects; known primitive metadata writes retain deferral.

`chunk_export_purity.<defining chunk>.inline_callback_factory_exports` is a
narrower named-export contract. It applies only to calls with exactly one
non-spread syntactic inline function or arrow callback. The provider must
return a fresh ordinary callable containing that callback, without invoking
it or any caller-, host-, or export-provided callback synchronously or
publishing it elsewhere. Its other synchronous behavior must satisfy the
same importer-state restriction above. The author audits provider and helper
paths reachable for this admitted input shape; other overloads are unclaimed.
Calls with a named callback, a retained factory result, spread arguments, or
`new` use conservative callback timing. This contract is independent of purity
and does not grant the broader `callback_factory_exports` composition rule.

### Retaining existing or multiple callbacks

`retained_callback_factory_exports` and `retained_callback_factory_members`
are **separate, stronger author assertions**. No provider name implies this
contract. Each entry lists exact audited signatures:

```yaml
chunk_export_purity:
  provider:
    retained_callback_factory_exports:
      factory:
        - { argument_count: 1, callback_arguments: [0] }
    retained_callback_factory_members:
      api:
        make:
          - { argument_count: 2, callback_arguments: [0, 1] }
```

Positions are zero-based. At least one position must be listed, every listed
position must be less than `argument_count`, and all arguments must be present.
Unlisted positions admit only the inert primitive syntax of `no_reentry_exports`.
Spread, construction, computed/chained/aliased callees, uncertain callback
values, and unlisted arities retain conservative timing. The older contracts
continue to admit their existing shapes independently when both are declared.

Listed positions admit inline functions/arrows, stable local named function
values, and retained audited-factory results. Every admitted callback contributes
to a single over-approximated returned callback graph; extracting any member or
invoking or publishing that graph can expose all those callbacks and their
lexical captures. The contract never proves callback purity.

The author must audit **both the provider and each admitted call site's input
graphs**. Unlike a fresh inline callback, an existing function has observable
identity, aliases, own properties and a prototype. All provider/type/helper paths
must refrain from synchronously invoking any caller/importer/host callback,
publishing or mutating any supplied graph, or accessing importer-owned lexical
state through any other route. The provider must return a fresh ordinary
callable/plain-object graph retaining the supplied callbacks, without introducing
additional provider callbacks, existing-instance aliases or cross-call state.
Provider-private effects remain allowed and normally ordered.

The callable property/prototype surfaces exposed through the result must contain
only inert metadata, standard intrinsic properties, and capabilities already
represented by the supplied callbacks and their lexical captures. Additional
callbacks attached as custom properties/prototypes, proxies and accessors are
outside the contract. A provider audit alone cannot establish this importer-side
obligation. Recognized mutation, rebinding and exposure keep the conservative
fallback; the checker does not prove the absence of all external aliases.

Namespace members retain the existing namespace identity, mutation and lookup
guards and require the same namespace capability audit as the fresh-only
contract. Result mutation and later callback invocation/publication retain the
ordinary synchronous-effect checks. No callback deferral assertion implies
purity or permission to reorder argument evaluation.

`debundle gate diagnose <binding>` provides a bounded source-backed trace for
one ownership target through constraining promoted callback dependencies. Its
supported rejection family is a matched imported namespace-member
callback-factory signature rejected by a top-level static write to that member
or a recognized alias, or by a computed-key write whose receiver is the
namespace binding or a recognized alias, or by the exact namespace binding
passed as a non-spread ordinary top-level call argument. The report names the
matched signature and exact source witness: write receiver/property or call
span/argument index, with a graph source-unit reference. An opaque-call label
requires a matching `UnknownCallTarget` loss at the same owner and span; the
guard alone does not prove a mutation occurred. A
supported row also requires the promoted edge's callee source unit to match a
statically resolved callback argument root from that exact signature.
Unresolved callback arguments and source-unit mismatches are inconclusive. The
report provides an explicit-path `inspect-source` command. Diagnose currently
does not read or fingerprint the source input, so it does not emit a parsed
statement ID or apply a possibly stale `SourceUnitOrdinal -> StatementId`
crosswalk. Five rows are returned by
default; `--all` requests exhaustive output, while query status summarizes all
uniquely linked candidate edges regardless of the output limit. This is a
local witness for this guard family, not a claim of a sole or globally earliest
cause. Computed-key writes through other receiver expressions, other guard
families, and absent promoted
dependencies are inconclusive. Multiple candidate barriers at one source unit
are also reported as an ambiguous link rather than cross-paired with callback
dependencies. Configured overloads are included only when their arity and
callback argument positions apply to the actual call.

### Retaining callbacks in constructed instances

`retained_callback_constructor_exports` is a separate author assertion for
direct `new` expressions through named or default imported bindings:

```yaml
chunk_export_purity:
  provider:
    retained_callback_constructor_exports:
      Box:
        - { argument_count: 3, callback_arguments: [0, 1] }
```

It uses the exact signature validation and callback/input-graph audit above.
Non-callback positions admit only inert primitive syntax. Spreads, namespace
member constructors, local constructor aliases, uncertain callback values and
unlisted arities keep conservative timing. This assertion does not apply to
ordinary calls; factory assertions do not apply to construction.

The author must audit construction, including superclass construction, field
initializers, default arguments and helpers: none may synchronously invoke,
inspect, coerce, mutate or publish the supplied callback graphs, or access
importer-owned lexical state through another capability. The imported
constructor's identity and construction behavior must remain stable during
synchronous initialization. The result must be a fresh instance retaining only
the supplied callback graphs, without preexisting-instance aliases or shared
callback storage across constructions. Its reachable methods, prototype and
property surfaces must introduce no unrepresented importer capabilities;
provider-private helper methods are allowed only under that same audit.
Proxies, accessors and externally mutable callback-bearing prototypes are
outside the contract. Intrinsic-integrity premises must also be established.

Recognized callback rebinding, mutation and exposure retain conservative
timing. Extracting an instance member, invoking it or publishing the instance
can expose every retained callback and its captures, including graphs returned
by those callbacks. Argument evaluation and provider-private effects retain
their normal checks and ordering. The contract does not assert construction
purity: `pure_new_exports` or `purity: pure_new` requires its own audit.

### Calls without synchronous reentry

`chunk_export_purity.<defining chunk>.no_reentry_exports` asserts that direct
calls of listed named imports cannot synchronously invoke caller-, importer-,
or host-provided callbacks or read importer-owned lexical state. The author
must audit all provider paths and preserve the exported callable's identity
through synchronous initialization; a provider-side rebind or mutable wrapper
that can change its behavior is outside the assertion. Provider-private effects
remain possible, and the result stays opaque. Only a direct imported binding
call with no spread or `new` and the same checked primitive argument forms as
`no_reentry_members` below is admitted; zero arguments are allowed. Argument
expressions are still visited normally.
Aliases, uncertain arguments, and result use retain ordinary reentry checks.
The defining module still initializes in its usual order. This contract is
independent of `pure_exports` and callback-factory assertions.

### Callback registration without synchronous reentry

`chunk_export_purity.<defining chunk>.no_reentry_callback_exports` is a separate
author assertion for direct imported calls with exact callback-bearing signatures:

```yaml
chunk_export_purity:
  provider:
    no_reentry_callback_exports:
      register:
        - { argument_count: 2, callback_arguments: [0] }
```

Positions are zero-based and at least one callback position is required. Every
unlisted argument must use the checked inert primitive syntax described above.
Listed arguments must resolve to stable local ordinary callable identities;
inline functions/arrows and stable named aliases are supported. Rebound,
exposed, mutated, uncertain or non-callable values, retained result graphs,
callee aliases, namespace calls, spread and construction remain conservative.

The author audits every admitted provider/type/helper path and each callable's
own/prototype surface and aliases. During the call, the provider must not invoke
any supplied, caller-, importer- or host-provided callback, mutate supplied
graphs, inspect effectful properties/prototypes or access importer-owned lexical
state. The exported callable must retain its audited identity and behavior
through synchronous initialization. Provider-private effects remain allowed.

Unlike the returned-graph factory contracts, this assertion permits storing
callbacks in cross-call provider registries or returning opaque capabilities.
After argument evaluation and the audited call, every supplied callback and its
capture graph becomes globally reachable in the analysis, even if the actual
provider only returns it. Every later opaque call or protocol operation may
invoke those callbacks. Sibling/operand evaluation and later invocation,
publication or mutation retain their ordinary checks. The result stays opaque.
This contract does not imply purity or any factory/constructor retention policy.

### Namespace and record calls without synchronous reentry

`chunk_export_purity.<defining chunk>.no_reentry_members` asserts a narrower
call-timing fact for listed static namespace members: with admitted primitive
arguments, the call does not synchronously invoke caller-, importer-, or
host-provided callbacks or read importer-owned lexical state. Provider-private
effects remain possible. It neither asserts purity nor describes the return
value; the return stays opaque. The same member must also satisfy
`namespace_independent_members` so its lookup cannot itself execute foreign
code, and the namespace identity and mutation checks must pass. The checked
call shape is a direct `namespace.member(...)` with no spread or `new` and only
primitive literals, `void` or `!` of a primitive literal, or a template with no
interpolations as arguments; zero arguments are allowed. Argument expressions
are still visited normally. Caller-owned objects, callbacks, aliases, optional
or computed lookups, and changed namespace members retain ordinary reentry.

`chunk_export_purity.<defining chunk>.no_reentry_record_members` is a separate
author assertion for direct static member calls with at least one fresh object
literal argument. Every other argument must be an admitted primitive value or
another fresh object literal. The objects may recursively contain those values
under static data keys; spread, accessors, methods, shorthand, computed keys,
and `__proto__` keys are excluded. This assertion requires an independent audit
of each admitted record shape and its provider branch, including any reads,
coercions, or retention of the object and its prototype. The provider must not
synchronously invoke caller/importer/host callbacks or access importer-owned
lexical state through the record. A primitive-only `no_reentry_members` assertion
does not imply this record assertion, and neither annotation is a general
whitelist for a named library. The same independent lookup, namespace identity,
mutation, and publication checks apply; the call result remains opaque.

`chunk_export_purity.<chunk>.namespace_independent_members` maps namespace
exports to individually audited static member names. Each lookup must be stable,
run no caller/importer/host code, and expose no capability to obtain the namespace
or replace an audited factory. An ordinary own data property or an explicitly
audited inert forwarding accessor can satisfy this; a primitive result alone
cannot. The guarantee covers the member value's reachable operations, properties,
prototypes, constructors, accessors, and returned callbacks, including implicit
namespace receivers in member calls and tags. Independently supplied argument or
receiver capabilities may propagate normally; the implicit namespace receiver is
not such an exception. Hidden closure capabilities to rewrite factories are
forbidden. This summary grants neither purity nor callback deferral.

The author must identify every imported export that can alias the namespace
family and retain the provider/runtime input audit below. The checker groups all
configured family roots together. Only when every runtime occurrence is the
receiver of a direct static lookup of a listed member may namespace admission
ignore unrelated unknown values and `this`. Whole namespace uses, writes,
updates, deletes, assignment/destructuring/loop targets, computed or optional
lookups, namespace exports, dynamic scope, or incomplete member lists retain the
ordinary conservative proof. Member results may be called, chained, constructed,
or used as superclasses with their ordinary execution effects. Callback deferral
still requires `callback_factory_members`; export/reentry and early callback
invocation checks remain active. See the [source-privacy proof](https://github.com/agentydragon/ducktape/blob/devel/devinfra/js/debundle/docs/design.md#namespace-capability-privacy).

Explicit member writes nested in object or array destructuring, including nested,
default, rest, and loop-head targets, retain the same conservative namespace
mutation checks as ordinary member assignments.

For a namespace-member contract, the author must also audit the provider and all
caller/runtime paths that supply values used by admitted callbacks. The listed
factory members must keep their audited identities and behavior throughout the
analyzed chunk's synchronous initialization. In particular, provider or other
namespace members, callback/ref/hook inputs, and values returned through those
paths must not expose an alias that can replace a listed factory member.
Direct static identifier-property namespace call results are treated as
independent only under this explicit author assertion. The analyzer propagates
explicit namespace provenance through assignments, local aliases, property
values, returns, and call arguments. An unrelated call or constructor result
with no such path is not treated as a namespace alias solely because its
runtime value is unknown; the author audit asserts that values used by admitted
callbacks cannot alias or replace listed members. Unknown syntax remains
conservative at publication and call-argument sinks. Binding cycles alone do
not establish namespace provenance; namespace and
unknown-value inputs to a cycle remain conservative. A static identifier-property
optional read directly from an identifier retains the receiver's namespace
provenance and the possible undefined result. Optional calls, computed optional
reads and chained receivers remain conservative; this value-flow rule does not
assert purity or remove accessor effects.
For call-argument checks,
the analyzer follows namespace values in the supplied argument expressions
and excludes the callee expression, which is not passed as an argument. A
value from an unknown call or constructor used as a static or dynamic write
receiver remains a possible namespace alias, including through local aliases
and member projections, and disables the contract. The checker does not prove
that arbitrary callback parameters or unrelated call results are disjoint
from the namespace; that non-aliasing guarantee is part of the author audit.
Under intrinsic integrity, adjacent writes to declared own data fields of a
checked fresh base-class instance do not invalidate a namespace contract.
The allocation and assignment values must run no user code, and the instance
must remain private throughout that run. Unsupported constructors, fields,
receivers, dynamic scope, and intervening statements retain the conservative
write checks.
For that exact top-level own-field write, the same proof excludes a setter
effect from unknown-call reachability. The assignment remains an actual
top-level effect; nested callable writes and later receiver mutations retain
their usual accessor checks.
For a fresh top-level object literal that visibly contains the namespace and
has static ordinary own data properties,
the checker distinguishes the container's identity from namespace values held
in its properties. A read of a verified own property follows that property's
retained value; storing a namespace in the container still counts as
publication at a sink. An adjacent write to an existing own data property may
use the same private fresh-value proof when its value runs no user code.
Getters, setters, computed keys, spreads, `__proto__`, unknown receivers, and
unsupported property reads retain conservative namespace checks. Literals
without an explicit namespace path keep the conservative receiver rule.
Class publication tracking also follows retained static fields through a
uniquely resolved local class superclass on the constructor surface. Unknown,
imported, reassigned, or non-class bases and cycles remain conservative;
instance inheritance, static accessors, decorators, and unsupported class
surfaces remain opaque.
Adjacent static property writes to the fresh ordinary result of a direct
audited factory call with one inline callback also retain the contract. Their
values must run no user code; prototype writes and intervening statements end
the proof. Rejecting the producing namespace's factory contract also removes
this proof from its results.

Under intrinsic integrity (A11), a sequence expression assigning a direct global
`Error(...)` result to a local binding can retain the namespace contract through
immediately following static property assignments to that binding. Values must
be inert or declared local reads; `__proto__`, `stack`, and intervening
expressions end the proof. Every matching write in that source unit and callable must
be proved. Calls, argument evaluation, and namespace publication through assigned
values retain their ordinary checks. Shadowed or visibly replaced `Error` and
dynamic scope remain conservative; see [design details](https://github.com/agentydragon/ducktape/blob/devel/devinfra/js/debundle/docs/design.md#conditions-on-the-input-chunk-assumptions-a1a11).

Within a literal traversed inside an admitted inline callback, a direct static
namespace member used as a call callee is analyzed as the callee expression,
not as a captured namespace value. Call arguments and effects remain analyzed;
computed and chained callees stay conservative.

Inside callback result object data properties, the analyzer also treats a
one-hop read such as `namespace.member` as a deferred member value. Only an
ordinary data property qualifies; the object-literal `__proto__` prototype
setter does not. That direct shape does not prove the member value's identity;
the author must audit its provider branches and any accessor, proxy, or
callback behavior reachable through it. Computed reads, chains, calls, spreads,
arrays, and other expression shapes remain conservative. Direct namespace
values and aliases remain rejected.

Without an assertion, source inference recognizes only immutable exported
function declarations with one simple identifier parameter. The body must
only return a zero-argument function or arrow that only calls that parameter
with no arguments. Reassigned bindings and dynamic scopes remain opaque.

### Deferred browser-global publication

`chunk_analysis_options.<chunk>.deferred_global_publications` names audited
browser-global data slots, for example `{globalThis: [inspectState]}` or
`{window: [inspectState]}`. A listed slot must be an ordinary inert own data
property, or an absent slot whose creation cannot invoke an inherited setter,
at the write. The object must permit the write. No host, importer, or source
consumer may use its stored function or class during synchronous
initialization of this chunk.
The analyzer admits only a whole top-level direct assignment of a known local
function/class, or the same `window.slot = value` assignment behind a checked
`typeof window` guard. It rejects ambiguous aliases, visible global-root
replacement, dynamic global access, additional source mentions of the slot
(including through another builtin
global alias), and visible global-object escapes. The certificate delays only
the callback-availability consequence of that exact write. RHS evaluation,
eager reads, property mutation, ordinary effects, and source-order constraints
remain. Independent exports or other publications of the same value retain
their own reentry behavior. A spec author must audit the host's property
descriptor and all consumers; source checks alone cannot establish either
host fact.
An earlier anonymous, zero-argument synchronous function IIFE with a discarded
result may use a local global-object alias before the certified write if that
alias and every derived value stay inside the invocation. Ordinary calls with
the alias, nested
callbacks, returns, dynamic direct member access, and certified-slot access
through the alias retain the conservative fallback.
Primitive-valued operator results cannot carry the global-object identity.
The synthetic protocol call for a literal-key `in` check on a direct builtin
global root does not publish that root as a value; its possible host or proxy
effects remain in synchronous effect analysis unless the independent audited
membership contract below applies. Dynamic keys, aliases, ordinary
calls, and visible root replacement retain the conservative publication guard.

## Version-scoped host call timing

`host_no_reentry` is an optional author assertion for two exact zero-argument
operations in an audited Bun standalone runtime. The existing Bun 1.3.10 profile
is:

```yaml
host_no_reentry:
  runtime: bun_standalone_1_3_10
  operations: [async_local_storage_construct, promise_resolve]
```

For a separately audited build, record its exact identity instead of reusing a
different release's profile:

```yaml
host_no_reentry:
  runtime:
    bun_standalone_build:
      version: "1.4.3"
      revision: "<40 hexadecimal characters from Bun.revision>"
      executable_sha256: "<64 hexadecimal characters for the audited executable>"
  operations: [async_local_storage_construct, promise_resolve]
```

Replace the identity placeholders with measured values. All three identity
fields are required; the version must be nonempty and contain no whitespace,
and the revision and executable digest must have the specified hexadecimal
lengths. These fields identify the author's audit. The analyzer validates their
format and retains them as evidence; it does not run or authenticate the host
executable, infer compatibility from a version, or transfer an audit between
builds. Keep the runtime/builtin provenance and audit with the spec.

The accepted operations are a direct `new` of a named `AsyncLocalStorage` import
from Bun's builtin `async_hooks` or `node:async_hooks`, and a direct ordinary
`Promise.resolve()` call on the unshadowed global constructor. The former
allows a named import alias (`as M`) but not a subsequent local alias; the
latter rejects a shadowed, aliased, replaced, computed, detached, or optional
callee. Both reject arguments and spreads. Visible global/`Promise` mutation
and dynamic scope disable the Promise contract. The assertions only suppress
the opaque synchronous callback-reentry fallback after the callee and
arguments have evaluated. Purity, ordering, possible throws, return shape,
later method calls, and module-loader reentry retain their ordinary analysis.
Omitting this field keeps generic/Node behavior conservative.

The author must verify that the analyzed program actually runs under the
specified Bun build with its original builtin loader, exports, Promise
constructor and descriptors. Earlier loaded modules, preloads, plugins,
patched binaries, and host instrumentation must not replace those capabilities
or introduce synchronous hooks before these initializers. The analyzer's
local mutation guard does not establish that full-program/runtime premise.
The version string alone is not a binary identity proof. The tagged Bun 1.3.10
[`async_hooks`](https://github.com/oven-sh/bun/blob/bun-v1.3.10/src/js/node/async_hooks.ts)
and [`v8.promiseHooks`](https://github.com/oven-sh/bun/blob/bun-v1.3.10/src/js/node/v8.ts)
stubs support this bounded assertion; [Node's `async_hooks`](https://nodejs.org/api/async_hooks.html)
can synchronously invoke registered callbacks during Promise creation, so the
assertion must not migrate to Node or another Bun build without a new audit.
Debugger-initiated evaluation and state changes are outside the behavior-equivalence
contract; ordinary program-accessible hooks and getters remain in scope.

Each successful use appears in the source unit's synchronous report under
`author_host_contract_uses`, with the selected runtime, exact operation and
source location. An unused policy produces no use record. These are static proof
uses, not a runtime invocation trace. Failed Promise/global integrity guards
remain visible under `native_integrity_checks` as `host_promise_resolve`. Host
assertions establish timing only; their provenance must not be presented as an
unconditional inferred native guarantee.

## Audited builtin-global membership probes

`chunk_analysis_options.<chunk>.non_reentrant_global_membership` maps builtin
roots (`globalThis` or `window`) to literal string keys. For example:

```yaml
non_reentrant_global_membership:
  globalThis: [featureEnabled]
```

This separately certifies the host's `[[HasProperty]]` protocol at every admitted
probe. The author must audit that the exact global object and its complete
prototype/alias surface remain intact throughout synchronous initialization,
including code in other chunks and host setup. The probe must neither invoke
or publish caller/importer/host callbacks nor access importer-owned lexical
cells through getters, proxies, modified prototypes or other hidden routes.
A browser-global name alone establishes none of these facts. Clean native
host behavior is a strong per-probe author premise, not inferred builtin trust.

Admission requires a listed literal string on the left of `in` and a direct,
unshadowed listed root on the right (parentheses are transparent). Keys with
unpaired surrogate code units cannot match the UTF-8 assertion and keep fallback.
Parameters, block-local bindings, imported roots, aliases, other roots, effectful receivers
and nonliteral keys keep their conservative protocol behavior. Recognized
root replacement, prototype writes, mutated Object intrinsics, dynamic global
member access, dynamic scope or global-root publication/escape also disable
the certificate. An earlier discarded synchronous function IIFE may use the
existing mechanically checked confined alias rule; its derived values cannot
escape or touch the protected keys or prototype/identity surface. Unsupported
or hidden alias/prototype mutations remain part of the author's audit burden.

A computed write to another object reached through a global property, such as
`globalThis.cache[key]`, does not alone identify a root/prototype mutation.
Explicit `__proto__`, `prototype` and `constructor` writes still reject, as do
computed writes on recognized global identity or constructor/prototype paths
(including `window.parent`, `document.defaultView` and `Window.prototype`).
Stored direct/recognized identity back-aliases (including nested record values)
remain rejecting. Storing a recognized prototype/constructor value on a
global-rooted receiver also rejects; merely holding that value in an unrelated
private object does not. The other root-exposure guards remain rejecting.
The author must establish that an otherwise admitted descendant cannot alias
the protected root or prototype surface; no host-property name proves this.
Indirect aliases through untracked variables, calls or host fields still need
the complete author audit; the path classifier does not prove their absence.
The descendant write retains its normal execution effects and callback checks.

Only protocol callback reentry is suppressed. Both operands and their existing
reads/effects are still evaluated and indexed in source order. Purity,
possible errors/throws, global-property dependencies and unrelated call effects
retain their existing treatment. The emitted JavaScript still executes the
original operator. Unlisted probes remain conservative even in the same chunk.
`deferred_global_publications` certifies a different timing boundary and never
implies this membership contract, or vice versa.

## Local-effect decorator annotations

`effect: typescript_decorate_helper` asserts target-local mutation; it does not
assert callback timing. `effect: typescript_metadata_decorate_helper` additionally
certifies that every admitted application, including every helper branch,
intrinsic alias and supplied decorator, only writes inert target metadata.
Decorators run synchronously, but neither they nor the helper may invoke target
methods, getters, setters or constructors; publish callbacks outside the target
graph; reenter unrelated caller/host/export callbacks; or access caller cells
through hidden routes. Provider-private constant/metadata reads are allowed;
observable writes outside the target graph are not. The author audits ordinary
intact targets, prototypes, descriptors and intrinsics. Existing descriptor,
prototype and callable behavior must remain unchanged. Imported decorators need
this explicit audit; purity or import shape does not imply their behavior.

The stronger summary applies only to a whole expression statement shaped as
`helper([bare identifiers], LocalClass.prototype, literalStringKey, 1|2)`, with
a nonempty spread-free array and a key other than `"__proto__"`. Analysis must
prove exact stable local function/class identities and initialized bindings.
Helpers with non-top-level captured state, dynamic scopes and unsupported forms
keep conservative timing. Eager argument reads,
the helper's and local decorators' syntactic lazy read/rebind dependency closure,
and target-local mutation/co-location edges remain. This is not purity.

`effect: typescript_metadata_decorate_helper_with_member_decorators` adds one
static member lookup (`provider.decorator`) to the admitted decorator array.
The provider root must be an imported binding without any direct member write
or rebind in the analyzed chunk. The author additionally certifies that each
admitted provider lookup is an ordinary, stable own data property throughout
synchronous initialization: no getter, proxy, alias mutation, or hidden
callback reentry can replace or observe it. The selected member must itself
be a metadata-only decorator under the contract above. This assertion covers
all supported applications of the helper, not just a single call. Computed
keys, chained members, spreads, and local providers remain conservative.
The existing bare-identifier helper effect does not acquire member support.

`effect: typescript_metadata_decorate_helper_three_args` explicitly asserts
the same metadata-only behavior for
`helper([bare identifiers], LocalClass.prototype, literalStringKey)`.
The helper determines the descriptor without a fourth kind argument. All
helper branches and supplied decorators must satisfy the contract above;
reading and restoring an ordinary own descriptor must preserve its behavior.
For a subclass target, inherited metadata must also be ordinary inert data:
copying it may neither invoke accessors nor mutate the base class's metadata.
Stable initialized identities, dependency closures and target co-location
checks still apply. Extra arguments and unsupported decorator/key expressions
remain conservative. Earlier effects retain their existing accepted shapes.

## Synchronous execution diagnostics

Dependency-graph source units carry `synchronous_execution` when synchronous
analysis ran. `complete` is the analyzer's existing effect-completeness verdict,
independent of `purity`. `precision_losses` retains the reasons observed when
execution became conservative: unresolved call targets, unsupported operations,
recursive invocations, and depth, expansion, or callback-pool limits. Limit
records include the configured bound. These records do not change gate acceptance
or emitted JavaScript.

Property-specific Object prototype premises appear in
`synchronous_execution.native_prototype_property_checks`: each entry names the
property, the absence verdict, and the number of blocking source operations.
The graph shares those operations once under `native_prototype_mutations`, with
chunk, source unit, source location, kind and possible property names. A null
property set means any name; an empty set means Symbol keys only. These are
conservative possible writers, not proof that a mutation executes. `describe`
selects blockers for checked names and applies its ordinary section limits.
Native-call identity checks and their witnesses remain separate.

`dependency_graph.json` includes `imported_callable_summaries` for directly
called bare imported callback-factory summaries used by synchronous analysis.
Each shared entry names
the importer's local binding and preserves all `callback_factory_evidence` from
the defining export, including asserted versus inferred origin. A source unit's
`synchronous_execution.callback_factory_uses` references the shared entry and
names the semantic use: `call_timing` suppresses opaque callback promotion for
an eligible call, `return_shape` retains its callback in a deferred result, and
`closed_result_root` omits a private initializer result from reachability roots.
Each use gives the lexical source-unit ordinal and call-site `source_location`;
the containing graph node is the top-level source unit whose initialization
analysis consumed it. Nested and repeated direct calls retain their lexical site and
deduplicate equal uses within that top-level analysis. Older graphs can omit
these fields. A recorded use means this analyzer applied the named capability;
it does not prove that the annotation was minimal or load-bearing for the gate,
nor enumerate other author-contract dependencies. Calls through local aliases,
joined values, and namespace-member contracts are outside this reporting scope.
An absent use is not a global unused-contract verdict.

Each loss identifies its lexical `statement_ordinal`. Its `source_location`
identifies the exact indexed operation when available; otherwise it identifies
the truncated callable or source unit. Nested execution carries its reasons to
the active top-level source unit; repeated and cached outcomes deduplicate equal
reasons at the same source site. A record describes an analysis fallback, not
proof that the operation executes on every runtime path. Historical graphs may
omit `synchronous_execution`. The loss list is diagnostic evidence, not an
exhaustive proof trace; `complete` remains authoritative even when the list is
empty.

## Vendor split-brain evidence

A partial vendor swap rejected for shared retained implementation reports both
its swapped-package ownership path and its residual liveness path. Ownership
traces distinguish dependencies of stripped exports from dependent items absorbed
into the package island. These explain the stripper's conservative classification;
they do not assert that every reported dependency executes synchronously.

## Native collection callback effects

An unexported `const` facade built by the exact nested
`Object.freeze(Object.defineProperty({__proto__: null, ...ownData},
Symbol.toStringTag, {value: primitive}))` shape retains its stored values
without invoking them. The `Object` and `Symbol` intrinsics must be intact;
keys and descriptor fields must be static own data, with no spread, accessor,
computed key, or intervening call. The analyzer keeps the facade's object
identity and property values, so a later call or publication still exposes its
stored callbacks. The proof changes only construction-time callback reachability;
ordinary eager dependencies, effects, and ordering remain.

Bounded synchronous analysis can execute a known callback for direct one-callback
`forEach`, `filter`, and `map` calls over a retained untainted dense ordinary
array. It passes actual retained element capabilities and preserves callback
reads, writes, coercions, and publication. Fresh results retain element aliases;
iteration and spread do not invoke stored functions. Unsupported sources,
mutation, intrinsic changes, and invocation limits retain conservative effects.
See [the proof and fallback conditions](https://github.com/agentydragon/ducktape/blob/devel/devinfra/js/debundle/docs/purity_soundness.md#retained-dense-array-iteration-effects).

A direct static `binding.push(...)` with non-spread primitive arguments may
also use a call-timing no-reentry proof when the binding resolves at
the current prefix to one untainted dense ordinary array. The existing Array,
Object and global native-integrity guards must hold. Callee/argument effects,
throws and heap ordering remain represented; the call remains impure and its
result remains unknown. This is not an array-like-object or general method
model. Own-method changes, prototype changes, unknown publication/escape,
spreads and uncertain arguments retain conservative callback effects.

After a proved push, the array's precise surface is invalidated and cached
invocations observe a new prefix revision. Its existing contained callbacks
remain reachable by later reads or opaque consumers. Subsequent array
operations use the conservative path rather than stale pre-push slots.
