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
   group is one request to the OR-Tools CP-SAT sidecar
   (`solver_backends/ortools_cpsat`), a required tool of the
   `debundle_pipeline` rule and a runfile of the `debundle` binary, over the
   program sliced to the group; requests run in parallel.

No constraint relates two chunks' entities, so every group lies in one chunk,
and a place is always one chunk's: two chunks never compete for it.

## Order

A chunk's `Resolution` lists its outcomes in a fixed order: entities rejected
before the solve (anonymous statements, then `source_matches[]` groups, then
single `source_match` members, each in module order), name pins with no
place, then the other anonymous statements, then the other members (name pins
and relational selectors, then `source_matches[]` groups, then single
`source_match` members, each in module order).

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

`undecided` means the sidecar stopped (its
`DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_MAX_TIME_SECONDS` limit, per request) before
deciding the entity. The sidecar reports which projected variables it had proven fixed by
then; an entity all of whose variables are among them still resolves, and a
conflict set found before the stop still stands.

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
the largest downstream spec chained 9,055 targets into one request and the sidecar was
killed for memory (2026-09-24). An entity whose rows all disagree with a
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

A contradiction stays inside its group, since each group is its own request.
Within the group it must not hide every other result either, so an
unsatisfiable request is localized to the targets that cause it
(`selector_backend_solver::solve_localizing_conflicts`).

The first compile presolves across targets
(`PresolveScope::AcrossTargets`): `all_different` propagates fixed values and a
relation table narrows both of its variables. That is what keeps requests small,
but a contradiction then surfaces as an empty domain wherever propagation met
it, not where it started. When that compile or its solve is unsatisfiable, the
program is recompiled within targets (`PresolveScope::WithinTargets`) and solved
again with every constraint attributed:

- **Ownership.** A target owns its owner variable and its binding variable. A
  constraint is attributed to every target owning one of its variables: a
  candidate table to its own target, a relation table to its owner and anchor,
  a `source_matches[]` group table to every target in the group, and each
  `all_different` entry to the target(s) owning that variable.
- **Hard constraints.** A constraint over variables no target owns carries no
  attribution and stays hard.
- **Presolve within targets** narrows a variable only from constraints attributed
  solely to targets owning it, and `all_different` propagates nothing. A target
  whose own constraints empty its domain gets an empty table instead of an empty
  domain.

The sidecar gives each target an assumption literal and enforces a constraint
only while every target it is attributed to is enabled; a disabled
`all_different` entry takes a value no other entry can. It takes CP-SAT's
sufficient assumptions for infeasibility as one conflict set, disables those
targets, and repeats until the rest is feasible, then solves the rest with the
conflicting targets disabled. CP-SAT's cores are not necessarily minimal, so a
conflict set may name a target that is not strictly needed for the
contradiction.

Each target in a conflict set of two or more comes out `conflict`, naming the
others; a set of one is that target's own constraints failing and comes out
`no_match`. Every other target resolves as usual. A target that depends on a
conflicting one, such as a relation anchored on it, loses that relation with it
and may come out ambiguous. Only when the hard constraints alone are
unsatisfiable does every target of the group come out `no_match`.

## Landing a new relation

1. Add the fact to `chunk_facts` if it is not derivable from what is there.
   Extraction stays fail-closed.
2. Lower it to a table over candidate ids in the resolve, with a compiled
   encoding in `selector_constraint_model_builder`.
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
<../selector_match_differential_test.rs> pinning the exact semantics.

Two properties make it near-linear rather than quadratic in selector count:
needle-only validation is hoisted out of the candidate loop, and exact-mode
identifier spellings are indexed so an identifier-only needle prunes through
postings instead of scanning every top-level statement. Scaling on a synthetic
corpus of the same shape class measures an exponent of ≈1.30 (synthetic
10k/40k-statement corpus, 2026-06-21).

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

Always record the compilation mode with a selector timing. A `fastbuild` binary
measured 5.1× slower than `-c opt` on the same `match-selector` probe, and 35×
slower on a historical proposer fixture — `fastbuild` numbers are not
comparable to anything.
