# Selector resolution

How the spec's selectors become one outcome per entity. What the outcomes
guarantee is <../SPEC.md>; this is how they are computed.

## One resolve

`selector_resolve::resolve` (<../selector_resolve.rs>) takes parsed chunks,
each with the spec entities scoped to it, and returns one `SelectorOutcome`
per entity. Every command that resolves selectors calls it, directly or as
`Chunk::resolve` of one chunk:

| Caller                                                    | Chunks and entities                                                                                                                                                    | Uses the outcomes                                                                                       |
| --------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `run` (`lowering/materialize/`)                           | every chunk, with every module of it (of every tree scoped to it), less what `run` claims itself: import-specifier pins, duplicate claims, pins on undeclared bindings | claims each resolved entity, records the rest (and elimination warnings) in `selector_diagnostics.json` |
| `spec validate --spec`                                    | as `run` (it is a keep-going dry run)                                                                                                                                  | reports every non-`ok` outcome and lists each matched template's free identifiers                       |
| `spec validate --source-file` (`cli/validate.rs`)         | one chunk file, with every module file                                                                                                                                 | reports every non-`ok` outcome and lists each matched template's free identifiers                       |
| `spec match-selector` (`match_selector.rs`)               | the probe alone                                                                                                                                                        | reports its outcome                                                                                     |
| `synthesize-selectors` proof (`selector_codemod.rs`)      | the candidate selector alone                                                                                                                                           | proven only when `resolved_by: own_selector` at the intended declaration                                |
| edit gate, `describe`, `peel` (`anonymous_resolution.rs`) | every chunk source the owner graph names, each with every module's `source_matches[]` and anonymous statements                                                         | an entity must resolve in one source and match in no other                                              |

A command that needs a selector unique on its own resolves it as a spec of one
entity: its outcome is then its own candidates' verdict.

## Inside the resolve

The resolve is two halves, so `run` can do the first per chunk in parallel:
`Chunk::project` (steps 1–3, one chunk) and `solve` (step 4, every chunk).

1. **Entities.** A member carries a name pin, a `source_match` template or a
   relational selector. `source_matches[].bindings[]` entries sharing one
   template form one group entity; an anonymous statement is an entity of its
   own. Each entity is scoped to its chunk, and a module is scoped to the
   chunk its tree names; several trees may name one chunk.
2. **Candidates.** The shape matcher (`ChunkResolver`) lists every place a
   template matches, and each place maps to its owner (a post-split top-level
   statement) and binding through the chunk's structural analysis. A template
   with no place is `no_match`, one with over `MAX_CANDIDATES_PER_SELECTOR`
   (100, `selector_outcome.rs`) is `too_broad`, and a matcher error or a place
   with no owner (an import specifier declares none) is `invalid` — all before
   any solve. A name pin's places are the top-level declarations of its name
   (of its kind); a pin with none is `no_match`. A `bindings[]` local the
   template uses without declaring (pinning by use site) is placed at each
   non-import top-level declaration of the identifier it bound in that match;
   a match where it bound none, or two identifiers, yields no row.
3. **Program.** Name pins and relational selectors lower to relation atoms over
   chunk facts (`selector_ir_lowering`); candidates enter as one table of rows
   per entity. `all_different` spans every non-pin target of the chunk, with
   one representative per group, whichever module or tree it comes from.
   Anchors of relational selectors resolve by export name within the chunk,
   except `intrinsic_alias`'s `referenced_by`, which resolves within the
   member's own module.
   `FactDomains` (`selector_constraint_model_builder`) derives exactly the
   relation tables the program's atoms read.
4. **Decision.** The program splits into groups of targets that interact: an
   atom relates their variables, or `all_different` keeps them distinct and
   they share a candidate owner or binding. A relational selector's places
   are unknown before the solve, so it interacts with every target
   `all_different` keeps it distinct from. A group that is exactly one
   `source_match` or anonymous-statement entity is decided from its own
   candidates, and a lone name pin with one place is a constant. Every other
   group is one CP-SAT problem over the program sliced to the group, solved
   inside the `debundle` process (§ The solver); groups solve in parallel.

No constraint relates two chunks' entities, so every group lies in one chunk,
and a place is always one chunk's: two chunks never compete for it.

## The solver

`selector_ortools_cpsat_backend.rs` implements `SelectorProblemBackend` over
OR-Tools' CP-SAT, linked into the `debundle` binary: a released `debundle` is one
file that needs nothing beside it. `selector_ortools_cpsat_model.rs` builds
OR-Tools' own `CpModelProto` (prost bindings of its `cp_model.proto`) from the
compiled problem, and `ortools_cpsat_ffi.rs` — the only `unsafe` in the crate —
calls CP-SAT's C API. The support search solves the model repeatedly, each time
forbidding what it has found, until every projected variable is proven fixed or
lists its alternatives (`MAX_ALTERNATIVES_PER_VARIABLE`).

| Variable                                             | Meaning                                                                                                                              |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_NUM_SEARCH_WORKERS` | Search threads of one CP-SAT solve, default 1. Groups solve concurrently, so the process runs up to that many times the group count. |
| `DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_MAX_TIME_SECONDS`   | CP-SAT's `max_time_in_seconds` for each solve of the support search (not for a whole group), default none.                           |

CP-SAT built without `NDEBUG` is about an order of magnitude slower and logs
`CP-SAT is running in debug mode` to stderr. The library is linked only through
`//devinfra/js/debundle:ortools_cp_solver`, which builds OR-Tools, protobuf and
Abseil with `--compilation_mode=opt` whatever the consumer's mode is
(<bazel_integration.md> § Solver build). That warning means a target links
`@or-tools//ortools/sat/c_api:cp_solver_c` around it.

Solving in process means a solve's failures are the process's:

- An OR-Tools `CHECK` failure, or a group that exhausts memory, ends the whole
  `debundle` process; `settle_references` keeping groups small is also what keeps
  a solve's memory down.
- CP-SAT checks `max_time_in_seconds` between presolve steps, so a step in
  progress runs past it: on synthetic 400,000-tuple table models a 0.5 s limit
  returned after 7.4 s and 7.9 s, and limits of 10 s to 60 s returned within 2 s
  of the limit (2026-09-30). `SolveCpStopSearch` from a watchdog thread is
  checked at the same points and returned no sooner, so none is used.
- CP-SAT's default SIGINT handler replaces the process's own for good, so the
  solver runs with `catch_sigint_signal` off and `^C` still ends `debundle`.
- A solve runs on a thread with an 8 MiB stack, not on the caller's (a rayon
  worker's is 2 MiB).

## Order

A chunk's `Resolution` lists its outcomes in a fixed order: entities the matcher
failed on (`invalid`), then entities rejected once their template references
narrowed their places (§ Template references: `no_match`, `too_broad`,
`conflict`, `invalid`), each set as anonymous statements, then `source_matches[]`
groups, then single `source_match` members, in module order; then name pins
with no place, then the other anonymous statements, then the other members
(name pins and relational selectors, then `source_matches[]` groups, then
single `source_match` members, each in module order).

`run` records outcomes in two passes over the chunks, each in chunk-id order:
first the claims it makes itself (duplicate claims, in request order), then,
once every chunk has resolved, each chunk's resolution outcomes followed by
its elimination warnings. That is the order `--fail-fast` stops in
(<../SPEC.md> § Modes).

## Outcomes

The kinds and their severities are <../SPEC.md> § Outcomes; the record and
its constants live in `selector_outcome.rs`. `MAX_LISTED_CANDIDATES` also bounds
the solver's alternative search (`MAX_ALTERNATIVES_PER_VARIABLE`), so an
`ambiguous` target lists what the solver found, not every place.

`undecided` means CP-SAT stopped (at its `DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_MAX_TIME_SECONDS`
limit) before deciding the entity. The solver reports which projected variables
it had proven fixed by then; an entity all of whose variables are among them
still resolves.

## Template references

Every template row, member or anonymous statement, carries `free_bindings`: the
chunk identifier each free template name bound throughout that match (a name
that bound two identifiers is absent). Before the solve, the resolve classifies each free name
(<../SPEC.md> § Matching) and narrows the entity's rows: a global must have
bound itself, a name-pin reference the pinned name, and a reference to a
projected `source_match` or relational entity must be present at all.

`settle_references` then repeats to a fixpoint: a referenced entity whose rows
all bind one name for that export filters its referencers' rows to that name
directly. Only a reference to an entity still open reaches the solver, as a
column of the referencer's candidate table over the referenced entity's binding
variable (`projected_binding_variable`), so equality comes from the shared
variable. Settling first keeps groups small: with a column for every reference,
the largest downstream spec chained 9,055 targets into one group and the solver
ran out of memory (2026-09-24). An entity whose rows all disagree with a
reference is `conflict` with the entities referenced.

After the solve, an entity that had several rows before its references narrowed
them resolves `resolved_by: own_references` when exactly one row agrees with
the solved bindings of the entities it references; otherwise elimination below
decides.

## Resolved by elimination

`all_different` can make a selector unique that is ambiguous on its own: its
other candidates are claimed by other selectors. After a solve, each unique
`source_match` or anonymous-statement entity's candidate rows are filtered by dropping every row whose
owner or binding another `all_different` target's solved value holds. When it
had several rows and one survives, its outcome is `resolved` with
`resolved_by: elimination` naming the claimers. When several survive and it
still resolved, a template that references it picked the place:
`resolved_by: referenced_by` naming those referrers. Either selector silently
moves when a claimer or referrer is edited, so it should be anchored on its
own.

## Nearest unclaimed

Once a chunk's outcomes are recorded, each `no_match` template entity gets the
top-level statements no entity of the chunk resolved to that its template comes
closest to (`add_nearest_unclaimed`, scored by `source_match::fact_near_misses`,
bounded by `NEAREST_UNCLAIMED_MIN_SCORE` and `NEAREST_UNCLAIMED_LIMIT`). It
runs after the solve because "unclaimed" needs every entity's result; only a
one-statement template that is not all holes is scored.

## Differentiators

Each `ambiguous` entity whose places are all listed then gets, per listed
place, the best-ranked anchor that sets its statement apart
(`add_differentiators`): a `ShapeIndex` built over just the listed places'
statements reads off, by `ShapeIndex::distinguishing_feature`, a feature no
other of them has — a literal, object key, class member, member-path call,
declaration kind or arity; never a shape skeleton, which names no token, nor a
volatile literal. A place with none is retried against the statements just
before the other places, then just after. Places that share a statement never
get one. The index covers at most `MAX_LISTED_CANDIDATES` statements per side,
so the pass stays within the interactive budget; with `truncated`, an unlisted
place might share the anchor, so none is given.

## Unsatisfiable programs

A contradiction stays inside its group, since each group is its own solve.
A group's program is unsatisfiable when compile-time presolve proves it
(`all_different` propagates fixed values and a relation table narrows both of
its variables until a domain or table is empty) or CP-SAT proves it
infeasible (`selector_backend_solver::solve_with_backend`). Every target of
the group then comes out `no_match` with one fixed `reason`: two or more of the
group's selectors claim the same place or contradict a relation, and which ones
is not determined. Other groups resolve as usual.

A `conflict` outcome is not a solver result: an entity whose rows all disagree
with a reference is rejected before the solve (§ Template references).

### Rejected: localizing a contradiction with assumption cores

Attributing every constraint to the targets owning its variables, presolving
within targets only, and re-solving with one assumption literal per target
reported each contradiction as a `conflict` among the targets of a CP-SAT core
(sufficient assumptions for infeasibility) while the rest of the group
resolved. It was dropped because each round is a full CP-SAT solve of a model
that presolve across targets no longer shrinks, and CP-SAT returns one core per
solve: the cost is (conflicts + 1) × the model's load time, not search.

Measured with an optimized CP-SAT on one real 96 KB problem, 216 targets in
one group, infeasible because two pairs of entities claimed the same
declaration: proving the plain across-targets program infeasible took 22 ms.
Localization ran three full solves, about 28 s each with presolve on (86 s in
all, finding the same two size-2 cores) and about 3 s each with
`cp_model_presolve:false` (9.4 s in all). Stack samples put the time in model
expansion and loading (`ExpandCpModel`, `FullyCompressTuples`, `LoadBaseModel`,
probing), not in search, and the `DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_MAX_TIME_SECONDS`
limit does not interrupt a presolve step (§ The solver). Duplicate claims are not a rare path:
authoring specs in parallel produces several at once.

## Landing a new relation

1. Add the fact to `chunk_facts` if it is not derivable from what is there.
   Extraction stays fail-closed.
2. Lower it through the selector program. `selector_ir`: a `SelectorAtom`
   variant with arms in `variable_ids`, `remap_variables` and `validate_atom`,
   and a `SelectorFact` variant for a new kind of fact. `selector_ir_lowering`:
   an arm of `lower_selector_atoms`. `selector_resolve`: a `selector_fact_store`
   branch that extracts its facts only when the program holds the atom.
   `selector_constraint_model_builder`: an arm of `lower_atom_constraint`,
   `DerivedFactRequirements::from_program` and `add_atom_constants`, and its
   tables in `FactDomains` and `RelationSupportCache`.
3. Prove it through `debundle run` on a fixture whose chunk also exports and
   uses the anchor, as <../e2e/cross_ref_lowering_test.rs> does: real graphs
   model `export { … }` and side-effect statements as owners that reference
   every binding they touch, which is the discriminating case bare fixtures miss.
4. Document it in <selectors.md>: a selector kind that is not documented there
   does not exist for authors.

## The shape matcher

`source_match/chunk_resolver.rs` builds one per-chunk model and resolves many
selectors against it, so a chunk with thousands of selectors pays setup once.
`selector_match` is the homomorphism itself: hole-skipping, alpha-equivalence
bijections, and run-hole subsequence alignment, with
<../selector_match_test.rs> pinning the exact semantics.

Two properties make it near-linear rather than quadratic in selector count:
needle-only validation is hoisted out of the candidate loop, and exact-mode
identifier spellings are indexed so an identifier-only needle prunes through
postings instead of scanning every top-level statement. Scaling on a synthetic
corpus of the same shape class measures an exponent of ≈1.30 (synthetic
10k/40k-statement corpus, 2026-06-21).

`STR_LITERAL_MATCHING_RE` predicates are compiled once per needle; compiling per
candidate was the hot path.

`chunk_facts` extraction is fail-closed: a construct it cannot project
faithfully is `Unsupported` rather than approximated.

### Rejected: tree matching as solver constraints

Encoding `source_match` as finite-domain constraints over AST nodes, so the
solver matches instead of the shape matcher, was measured on the largest known
downstream chunk (6.81 MiB) and abandoned. With `-c opt` binaries the matcher
resolved all 6,179 `source_match` selectors in 11.1s warmed; the native encoding
took 7.1s for one, almost all of it model construction (the CP-SAT search of
that request took 0.02s), and a whole spec timed out at 120s before reaching
the solver. A finite-domain solver has no index over AST shape, so the encoding
rebuilds what `selector_match::Index` already provides. Template references do
not need it either: they share the referenced entity's binding variable
(§ Template references), and negation and counting (not expressible yet,
<selectors.md> § Relational selectors) would be relation atoms over chunk facts. Measured `-c opt` on a 4-core host, 2026-09-17; the
ratio is the result.

### Rejected: binding free template names in the root frame

Binding a template's free names (referenced, never declared) in the matcher's
root frame would make one free name mean one chunk identifier across the whole
template. The root frame's bijection then rejects real templates that rename a
declaration yet reference it by its chunk spelling: in
`const wrap = () => use(q), readable = () => 1;` against the chunk's
`const w = () => use(q), q = () => 1;`, the free `q` and the declared `readable`
both need chunk `q`. On the largest downstream spec that turned 16 resolved group bindings
into `no_match` (2026-09-24). Free names bind in their frame like any other
reference; the matcher only records what each bound to, and reports a name that
bound two different identifiers as unbound.

## Interactive budget

Root `AGENTS.md` § Profiling applies. Interactive commands target under 10s on
warmed inputs for the largest known downstream specs; sustained runs over 60s
are priority bugs unless the command is an explicit offline/profile mode.
Matcher and index changes show a material wall-time drop on a broad workload,
not only a microbenchmark win.

Always record the compilation mode with a selector timing. A `fastbuild` binary
measured 5.1× slower than `-c opt` on the same `match-selector` probe, and 35×
slower on a historical proposer fixture — `fastbuild` numbers are not
comparable to anything.
