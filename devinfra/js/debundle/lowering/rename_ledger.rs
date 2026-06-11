//! `RenameLedger` — the collect → seal → execute-once rename pipeline.
//!
//! Every rename a lowered chunk applies flows through one architectural
//! boundary: contributors submit [`RenameIntent`]s during the collect
//! phase instead of building their own maps, [`RenameLedger::seal`]
//! validates the intents and freezes them into the read-only
//! [`SealedRenames`], and one post-seal executor pass per scope unit is
//! the only mutation of that unit's AST. No pass invents a rename after
//! seal; consumers that bridge pre- and post-rename names consult the
//! sealed maps (e.g. `RuntimeImportLookup::original_by_renamed`, the
//! inverse projection — injective by seal's target-collision rules).
//! There is no pre-seal trial application: capture facts reach seal from
//! read-only walks of the **un-renamed** body. This makes the
//! "X-layer renamed it, Y-layer keyed off the wrong-era name" bug class
//! unrepresentable rather than defended against.
//!
//! ## Scopes, origins, priorities
//!
//! An intent renames `from` to `to` within one [`RenameScope`]
//! (`Chunk` | `Module` | `Function` | `EntryPublicExports`); intents in
//! one scope are invisible to queries for any other scope. The
//! [`RenameOrigin`] names the contributor (for conflict diagnostics)
//! and derives the conflict priority: explicit (spec-author decision) >
//! import-induced (mechanical, required to emit valid imports) >
//! heuristic (cosmetic readability).
//!
//! ## Seal validation
//!
//! Seal is the single validation point for ledger renames:
//!
//! - **Conflicts**: no two same-priority intents may disagree on one
//!   `(scope, from)` binding's target — a hard error naming both
//!   contributors. A higher-priority intent (explicit > import-induced >
//!   heuristic) silently wins over a disagreeing lower-priority one.
//! - **Target occupancy**: callers pass per-scope [`ScopeOccupancy`]
//!   facts in [`SealValidation`]; seal rejects targets the scope already
//!   binds. Explicit intents failing validation are hard errors carrying
//!   the same messages the pre-ledger application-site checks raised
//!   (`invalid chunk_renames spec`, `collides with an existing top-level
//!   local`, `would be captured by a nested binding`, …). Heuristic
//!   intents failing validation are dropped silently (over-suppression is
//!   acceptable; capture is not). Import-induced intents are minted by
//!   the ledger itself against the same occupancy, so a failure is an
//!   internal invariant violation.
//! - **Capture facts, not conservative sets**: capture is
//!   reference-precise — a target bound in a nested scope is harmless
//!   when the source is shadowed (or never referenced) inside that scope,
//!   and the e2e suite pins valid specs of exactly that shape. A flat
//!   occupied-name set cannot express this, so the caller supplies the
//!   `(source, target)` pairs a scope-aware walk of the **un-renamed**
//!   body withholds ([`ScopeOccupancy::Body::captured`]): the read-only
//!   `RenameCaptureProbe` over [`RenameLedger::pending_renames_by_name`]
//!   for the entry body, the derive clone's candidate walk for module
//!   bodies. Seal turns them into the hard error; the post-seal executor
//!   `debug_assert!`s its own capture set is empty, pinning probe and
//!   executor to one verdict.
//!
//! Scopes without an entry in `SealValidation::occupancy` get conflict
//! validation only — used by the chunk-level explicit ledger sealed
//! before lowering, whose intents are re-collected and occupancy-checked
//! by the downstream ledgers once the post-split bodies exist.
//!
//! ## Name minting
//!
//! The ledger owns "names taken in scope": callers seed each scope's
//! taken set from the body's occupied names ([`RenameLedger::seed_taken`]
//! / [`RenameLedger::claim`]) and request fresh names with
//! [`RenameLedger::mint`], which suffix-mints `base$N` past collisions
//! (the `$N` scheme is the minting contract; readability is a later
//! naturalizer concern). Import-local disambiguation (`import_emit.rs`)
//! and public-export growth (`exports.rs`) mint through the ledger.
//!
//! ## Contract: no structural moves between seal and execute
//!
//! Once a chunk's ledger is sealed, no pass may move declarations
//! between modules — structural moves (entry-body split, residual
//! sweep, rebind folds, mini-factor synthesis) all happen before
//! collection finishes. The materializer satisfies the collection side
//! (intents are collected from the finalized `ChunkPlan`), and every
//! ledger executes post-seal only.
//!
//! ## The four ledgers
//!
//! Four ledger instances cover a chunk's lowering. Each follows the
//! same discipline — collect every intent, seal once, execute once —
//! and each is a separate instance for a structural reason, not code
//! shape:
//!
//! - **Chunk explicit ledger** (`materialize_logical_chunk`): spec
//!   `chunk_renames` + plan `export_name`s; conflict-validated only (the
//!   post-split bodies its targets must avoid don't exist yet — its
//!   intents are re-collected into the two body-validating ledgers
//!   below). It cannot merge downstream: its sealed Chunk projection is
//!   an input to plan building and factorization, which run before the
//!   post-split bodies exist.
//! - **Entry ledger** (`lower_chunk`): the chunk_renames entries staying
//!   in entry plus entry import-local mints (`Chunk` scope) AND the
//!   auto-grown residual public exports (`EntryPublicExports` scope — a
//!   separate namespace from local-binding renames). One seal validates
//!   entry's post-split body occupancy (capture facts from the read-only
//!   `RenameCaptureProbe`) and the public-export namespace; one executor
//!   pass applies the Chunk map to the entry body.
//! - **Per-module naturalize ledger** (`naturalize_module_body`): the
//!   plan's export_name renames (Explicit), free-source return-object
//!   aliases (Heuristic, `Module` scope), and bound-source scope-local
//!   heuristics (Heuristic, `Function` scope) — one seal resolves
//!   explicit-vs-heuristic priority, the module-level target-collision
//!   rule ([`merge_module_renames`]), and per-scope occupancy. The
//!   executor (sealed module-wide walk + `SealedScopeRenameApplier`) is
//!   the only mutation of the real body; derivation runs on a scratch
//!   clone of the un-renamed body (see "Derive clone").
//! - **Per-plan import ledger** (`lower_single_plan`): cross-module +
//!   residual-entry import-local mints. Separate from the naturalize
//!   ledger because of a cross-module phase cycle, not code shape:
//!   collection needs this module's post-naturalize body facts AND
//!   entry's grown export list, which itself needs every module's
//!   post-naturalize facts — the naturalize seal would have to stay
//!   open across a chunk-wide phase whose inputs require the naturalize
//!   application to have already run. Collapsing the two would mean
//!   planning references off un-renamed facts plus sealed-map reasoning
//!   at import emission, which changes mint seeding and is not
//!   behavior-preserving in suffix-mint corner cases; the two-ledger
//!   split is the accepted design.
//!
//! ## Boundaries that validate at application
//!
//! Two scope-aware checks live at application sites by design — they
//! validate facts only the application point can see, and are part of
//! the architecture rather than residual defense:
//!
//! - **`chunk_renames` over moved module bodies**
//!   (`lower_single_plan`'s `cross_module_chunk_renames` pass): the
//!   entry-side seal validates against entry's post-split body only, so
//!   a target can still be captured inside a moved body; that
//!   application's capture check is the moved body's only occupancy
//!   validation for spec renames and keeps its reachable bail. The pass
//!   also deliberately composes *sequentially* with the import ledger's
//!   mints rather than sealing with them: an import-local mint may
//!   rename `x → y$1` precisely because the chunk-rename target `y` was
//!   taken, and the cross map's `x → y` must then no-op on the
//!   already-renamed refs — one seal's priority rule would resolve the
//!   pair to the explicit target and desync body refs from the emitted
//!   import local.
//! - **Free-alias function-local application**: free-source
//!   return-object aliases rewrite references function-locally while
//!   their intents live at `Module` scope (they join the module-wide
//!   merged map so runtime-import planning can reverse-resolve them).
//!   The module-level occupancy of their targets is deliberately not
//!   checked; the deriving subtree's bound-name rule — applied at seal
//!   to their `Function`-scope copies — is the capture guard. This is
//!   also why `NaturalizedRenames` keeps its `merged` / `explicit`
//!   split: export locals and binding-comment keys may remap only
//!   through renames that actually rename a top-level declaration.
//!
//! ## Derive clone
//!
//! `naturalize_module_body` derives heuristics on a scratch clone of
//! the un-renamed body: the per-scope derive cascade needs each
//! enclosing scope's subtree name facts to reflect the nested scopes'
//! fired renames (the application is scope-sensitive — a flat set
//! transformation of the sealed nested maps over the un-renamed facts
//! both over- and under-suppresses), so the clone walk is the precise
//! fact source. The clone's local preview applies the same rules seal
//! re-derives from the submitted facts, so clone and sealed output
//! agree; the clone never touches the real body, whose only mutation is
//! the post-seal executor. The clone's plan-driven walk doubles as the
//! Module-scope capture probe.
//!
//! ## Hygiene boundary
//!
//! Intents are keyed by hygiene-aware [`Id`] — post-#2042 the chunk AST
//! carries real `SyntaxContext`s and bare-string keys are how rename bugs
//! breed. Contributors that only have a spec string resolve it at the
//! collection point via `top_level_id(name, chunk_top_level_mark)`. The
//! seal output is projected back to bare syms at the query boundary
//! (`*_by_name`) because the application visitors are string-keyed; the
//! projection asserts that no two hygiene contexts share a sym within
//! one scope — that assert is the boundary's tripwire. Deleting the
//! projection (an `Id`-keyed executor) requires emitting import/export
//! decls under real contexts instead of `Ident::new_no_ctxt` (today one
//! rename must hit both a no-ctxt emitted import local and the
//! real-ctxt body references, which only sym-keyed application can do)
//! and hygiene-resolving the `Function`-scope heuristic sources; see
//! TODO.md "Rename pipeline".
//!
//! `Function`-scope heuristic sources are function-local bindings whose
//! hygiene context the string-keyed derivation never resolves; they are
//! keyed by `(sym, SyntaxContext::empty())`. Within one function scope a
//! sym maps to at most one rename, so the empty-context encoding cannot
//! collide.
//!
//! ## Contributor inventory
//!
//! Every rename contributor submits intents and all validation is
//! seal-time:
//!
//! - Spec `chunk_renames` — `chunk_renames.rs::collect_chunk_renames`
//!   (scope: `Chunk`, origin: `Explicit`; chunk explicit ledger, then
//!   re-collected into the entry ledger which validates occupancy).
//! - Plan-driven spec `export_name`s —
//!   `naturalize.rs::collect_plan_export_rename_intents` (scope:
//!   `Module`, origin: `Explicit`; chunk explicit ledger, then
//!   re-collected into the per-module naturalize ledger which validates
//!   occupancy).
//! - Heuristic bound-source scope-local renames — `naturalize.rs`,
//!   derived per function-like node by `ScopedHeuristicNaturalizer`
//!   (scope: `Function`, origin: `Heuristic`); raw candidates are
//!   submitted with the deriving subtree's name facts and seal applies
//!   the validity rules ([`ScopeOccupancy::Subtree`]).
//! - Heuristic free-source return-object aliases — `naturalize.rs::
//!   collect_free_alias_renames_from_item`, submitted per deriving
//!   function (scope: `Module`, origin: `Heuristic`; same per-module
//!   ledger; the module-global target-collision rule runs at seal via
//!   [`merge_module_renames`]).
//! - Import-local disambiguation (fresh-local `$N` minting) —
//!   `import_emit.rs::disambiguate_*` mint through
//!   [`RenameLedger::mint`]. The entry-side call site in
//!   `lower.rs::lower_chunk` submits into the entry ledger (scope:
//!   `Chunk`, origin: `ImportInduced`); the module-side call sites in
//!   `imports_cross.rs` submit into the per-plan import ledger (scope:
//!   `Module`, origin: `ImportInduced`).
//! - Collision-resolving public-name minting —
//!   `exports.rs::auto_grown_residual_exports` (mints a grown public
//!   export past pre-existing public names via [`RenameLedger::mint`];
//!   scope: `EntryPublicExports`, origin: `ImportInduced`; sealed in
//!   `lower_chunk`'s export-growth phase).
//!
//! Vendor boundary renames (`vendor/`) run in a separate pipeline stage on
//! different artifacts and are out of this ledger's scope.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt;

use analysis::ModuleId;
use anyhow::{Result, bail};
use swc_atoms::Atom;
use swc_common::Span;
use swc_ecma_ast::Id;

use super::util::is_valid_js_identifier;

/// Identity of one function-like deriving scope (function / arrow /
/// constructor): the node's source span in the chunk AST. The per-scope
/// heuristic machinery (#2057's `ScopedHeuristicNaturalizer`) identifies a
/// deriving scope as the function-like node it is visiting; the span is
/// that node's persistent key — a chunk AST parses from a single
/// `SourceFile`, so two distinct function-like nodes never share a span.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct FunctionScopeId {
    pub lo: u32,
    pub hi: u32,
}

impl From<Span> for FunctionScopeId {
    fn from(span: Span) -> Self {
        Self {
            lo: span.lo.0,
            hi: span.hi.0,
        }
    }
}

/// Where a rename applies. Intents in one scope are invisible to queries
/// for any other scope — the ledger rejects cross-scope leakage by
/// construction.
///
/// Variant order matters for seal: `Module` validates before `Function`
/// (the free-alias survival check reads the Module-scope survivors).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum RenameScope {
    /// Bindings staying in the chunk's residual entry body.
    Chunk,
    /// Module-wide renames within one emitted logical module. The identity
    /// is the same [`ModuleId`] (index into the finalized `module_plans`
    /// list) that `binding_assignment` and the factorization partition use
    /// to identify a binding's deriving module.
    Module(ModuleId),
    /// Scope-local renames derived by one function-like node.
    Function(FunctionScopeId),
    /// Public export-name allocations on the chunk's residual entry — a
    /// separate namespace from local-binding renames: `from` is the
    /// residual binding's top-level `Id`, `to` the public name entry's
    /// grown `export { … }` clause assigns it
    /// (`exports.rs::auto_grown_residual_exports`).
    EntryPublicExports,
}

impl fmt::Display for RenameScope {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RenameScope::Chunk => write!(f, "chunk scope"),
            RenameScope::Module(ModuleId(index)) => write!(f, "module #{}", index.0),
            RenameScope::Function(span) => write!(f, "function@{}..{}", span.lo, span.hi),
            RenameScope::EntryPublicExports => write!(f, "entry public exports"),
        }
    }
}

/// Which kind of contributor proposed a rename. Carries the contributor
/// name so seal-time conflicts can name both sides.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum RenameOrigin {
    /// Spec-author decision (chunk_renames member, plan `export_name`).
    Explicit { contributor: &'static str },
    /// Mechanical rename required to emit valid imports (fresh-local
    /// minting, cross-module import aliases).
    ImportInduced { contributor: &'static str },
    /// Cosmetic readability rename inferred from the AST.
    Heuristic { contributor: &'static str },
}

impl RenameOrigin {
    pub fn priority(&self) -> RenamePriority {
        match self {
            RenameOrigin::Explicit { .. } => RenamePriority::Explicit,
            RenameOrigin::ImportInduced { .. } => RenamePriority::ImportInduced,
            RenameOrigin::Heuristic { .. } => RenamePriority::Heuristic,
        }
    }

    pub fn contributor(&self) -> &'static str {
        match self {
            RenameOrigin::Explicit { contributor }
            | RenameOrigin::ImportInduced { contributor }
            | RenameOrigin::Heuristic { contributor } => contributor,
        }
    }
}

impl fmt::Display for RenameOrigin {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RenameOrigin::Explicit { contributor } => write!(f, "explicit({contributor})"),
            RenameOrigin::ImportInduced { contributor } => {
                write!(f, "import-induced({contributor})")
            }
            RenameOrigin::Heuristic { contributor } => write!(f, "heuristic({contributor})"),
        }
    }
}

/// Conflict-resolution rank derived from [`RenameOrigin`]: explicit >
/// import-induced > heuristic (variant order ascending).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum RenamePriority {
    Heuristic,
    ImportInduced,
    Explicit,
}

/// One contributor's proposal to rename `from` to `to` within `scope`.
#[derive(Debug, Clone)]
pub struct RenameIntent {
    pub scope: RenameScope,
    /// Hygiene-aware source binding. Contributors that only have a bare
    /// spec string resolve it at the collection point via
    /// `top_level_id(name, chunk_top_level_mark)`.
    pub from: Id,
    pub to: Atom,
    pub origin: RenameOrigin,
}

/// Per-scope name-occupancy facts seal validates rename targets against.
#[derive(Debug)]
pub enum ScopeOccupancy {
    /// `Chunk` / `Module` / `EntryPublicExports` scopes: occupancy of one
    /// emitted body (or public-export namespace).
    Body {
        /// Identity used in error messages: the chunk id for `Chunk`
        /// scope, the plan id for `Module` scopes.
        label: String,
        /// Names bound at the scope's root (top) level. An explicit
        /// rename target colliding here is a hard error unless the name
        /// is vacated — i.e. it is itself the source of another explicit
        /// rename in the same scope.
        root: BTreeSet<String>,
        /// Names bound strictly below the root level
        /// (`scope_names::collect_nested_binding_names`). Ledger-minted
        /// targets must avoid them (mints claim from a taken set seeded
        /// with both levels; a collision is an internal invariant
        /// violation).
        nested: BTreeSet<String>,
        /// `(source, target)` pairs the rename visitor's scope stack
        /// withheld on the pre-seal application/preview walk of this
        /// scope's body: the target was shadowed at a reference of the
        /// un-shadowed source, so applying the rename would capture.
        /// Seal turns these into the hard "would be captured by a
        /// nested binding" error.
        captured: BTreeSet<(String, String)>,
    },
    /// `Function` scopes: the deriving subtree's name facts. Seal's
    /// per-scope rules over them are the same rules the derive clone's
    /// local preview applies (`validated_bound` + the free-copy
    /// bound-target filter), so clone and sealed output agree.
    Subtree {
        /// Names bound anywhere under the deriving node. Classifies each
        /// intent (bound-source vs free-source) and rejects free-alias
        /// targets the subtree binds.
        bound: BTreeSet<String>,
        /// Every value/binding identifier sym mentioned in the subtree.
        /// Bound-source heuristic targets must be absent from it.
        mentions: BTreeSet<String>,
    },
}

/// Validation context for [`RenameLedger::seal`]. Scopes absent from
/// `occupancy` get conflict/priority validation only.
#[derive(Debug, Default)]
pub struct SealValidation {
    pub occupancy: BTreeMap<RenameScope, ScopeOccupancy>,
    /// Names the module-wide renames reserve (sources + targets of the
    /// module's explicit + surviving free renames). `Function`-scope
    /// bound-source heuristic targets must avoid them.
    pub reserved: BTreeSet<String>,
}

/// Accumulates [`RenameIntent`]s during the collect phase and owns the
/// per-scope taken-name sets behind [`Self::mint`]; consumed by
/// [`Self::seal`].
#[derive(Debug, Default)]
pub struct RenameLedger {
    intents: Vec<RenameIntent>,
    taken: BTreeMap<RenameScope, BTreeSet<String>>,
}

impl RenameLedger {
    pub fn submit(&mut self, intent: RenameIntent) {
        self.intents.push(intent);
    }

    /// Seed `scope`'s taken-name set with names [`Self::mint`] must avoid
    /// (the body's occupied names, pre-existing public exports, …).
    pub fn seed_taken(&mut self, scope: RenameScope, names: impl IntoIterator<Item = String>) {
        self.taken.entry(scope).or_default().extend(names);
    }

    /// Claim `name` in `scope` so later mints avoid it; returns whether
    /// the name was newly claimed.
    pub fn claim(&mut self, scope: RenameScope, name: &str) -> bool {
        self.taken
            .entry(scope)
            .or_default()
            .insert(name.to_string())
    }

    /// Mint a fresh name in `scope`: `base` itself when it is a usable
    /// identifier and untaken, else the first untaken `base$N`. The
    /// minted name is claimed.
    ///
    /// A reserved word (`default`, `class`, `await`, …) used verbatim as
    /// a local would surface straight into an emitted `import {...}` /
    /// `export {...}` clause and produce un-parseable JS. Suffixing turns
    /// it into a valid identifier (`default$1`), so only offer `base`
    /// directly when it is a usable identifier. `$`-suffixed candidates
    /// are always valid, so the loop below always terminates with a
    /// parseable name.
    pub fn mint(&mut self, scope: RenameScope, base: &str) -> String {
        let taken = self.taken.entry(scope).or_default();
        if is_valid_js_identifier(base) && taken.insert(base.to_string()) {
            return base.to_string();
        }
        let mut suffix = 1usize;
        loop {
            let candidate = format!("{base}${suffix}");
            if taken.insert(candidate.clone()) {
                return candidate;
            }
            suffix += 1;
        }
    }

    /// Pre-seal projection of the collected intents for `scope`: per
    /// source sym, the highest-priority proposed target (same-priority
    /// disagreement resolves arbitrarily here — seal is the validator
    /// and hard-errors on it before any output is consumed). Callers use
    /// this to run the read-only capture probe over the un-renamed body
    /// so seal receives reference-precise capture facts without a trial
    /// application; on a successful seal the projection equals the
    /// sealed by-name map (debug-asserted at the call sites).
    pub fn pending_renames_by_name(&self, scope: &RenameScope) -> BTreeMap<String, String> {
        let mut best: BTreeMap<String, (Atom, RenamePriority)> = BTreeMap::new();
        for intent in &self.intents {
            if intent.scope != *scope {
                continue;
            }
            let priority = intent.origin.priority();
            match best.entry(intent.from.0.to_string()) {
                std::collections::btree_map::Entry::Vacant(slot) => {
                    slot.insert((intent.to.clone(), priority));
                }
                std::collections::btree_map::Entry::Occupied(mut slot) => {
                    if priority > slot.get().1 {
                        slot.insert((intent.to.clone(), priority));
                    }
                }
            }
        }
        best.into_iter()
            .map(|(from, (to, _))| (from, to.to_string()))
            .collect()
    }

    /// Validate and freeze the collected intents.
    ///
    /// Conflict resolution per `(scope, from)` group: the highest-priority
    /// intents must agree on one target — disagreement at equal priority
    /// is a hard error naming every contributor on each side.
    /// Lower-priority disagreement loses silently. Identical duplicates
    /// collapse. Every conflict in the ledger is reported in one error,
    /// not just the first.
    ///
    /// Target validation runs per scope against `validation.occupancy`
    /// (see the module doc's "Seal validation" section): explicit
    /// failures are hard errors reproducing the pre-ledger messages,
    /// heuristic failures are silent drops, import-induced failures are
    /// internal invariant violations.
    pub fn seal(self, validation: &SealValidation) -> Result<SealedRenames> {
        let mut groups: BTreeMap<(RenameScope, Id), Vec<RenameIntent>> = BTreeMap::new();
        for intent in self.intents {
            groups
                .entry((intent.scope, intent.from.clone()))
                .or_default()
                .push(intent);
        }
        let mut by_scope: BTreeMap<RenameScope, BTreeMap<Id, (Atom, RenamePriority)>> =
            BTreeMap::new();
        let mut conflicts = Vec::new();
        for ((scope, from), intents) in groups {
            let top_priority = intents
                .iter()
                .map(|intent| intent.origin.priority())
                .max()
                .expect("groups hold at least one intent");
            // Distinct targets proposed at the top priority, each with the
            // (sorted, deduped) contributors proposing it — a BTreeSet so
            // the rendered conflict is independent of submission order.
            let mut by_target: BTreeMap<&Atom, BTreeSet<RenameOrigin>> = BTreeMap::new();
            for intent in &intents {
                if intent.origin.priority() == top_priority {
                    by_target
                        .entry(&intent.to)
                        .or_default()
                        .insert(intent.origin);
                }
            }
            if by_target.len() > 1 {
                let sides = by_target
                    .iter()
                    .map(|(to, origins)| {
                        let origins = origins
                            .iter()
                            .map(|origin| origin.to_string())
                            .collect::<Vec<_>>()
                            .join(", ");
                        format!("`{to}` (per {origins})")
                    })
                    .collect::<Vec<_>>()
                    .join(" vs ");
                conflicts.push(format!(
                    "{scope}: binding `{}` renamed to {sides} at equal priority; \
                     same-priority contributors must agree",
                    from.0,
                ));
                continue;
            }
            let to = by_target
                .keys()
                .next()
                .expect("non-conflicting groups have exactly one target");
            by_scope
                .entry(scope)
                .or_default()
                .insert(from, ((*to).clone(), top_priority));
        }
        if !conflicts.is_empty() {
            bail!(
                "conflicting rename intents:\n  - {}",
                conflicts.join("\n  - "),
            );
        }

        // Target validation, scope by scope. `Module` scopes validate
        // before `Function` scopes (RenameScope variant order), so the
        // free-alias survival check below sees the Module survivors.
        let mut surviving_module_heuristics = BTreeSet::<(String, String)>::new();
        let mut sealed: BTreeMap<RenameScope, BTreeMap<Id, (Atom, RenamePriority)>> =
            BTreeMap::new();
        for (scope, winners) in by_scope {
            let kept = match validation.occupancy.get(&scope) {
                None => winners,
                Some(ScopeOccupancy::Body {
                    label,
                    root,
                    nested,
                    captured,
                }) => validate_body_scope(&scope, label, root, nested, captured, winners)?,
                Some(ScopeOccupancy::Subtree { bound, mentions }) => validate_function_scope(
                    bound,
                    mentions,
                    &validation.reserved,
                    &surviving_module_heuristics,
                    winners,
                ),
            };
            if matches!(scope, RenameScope::Module(_)) {
                for (from, (to, priority)) in &kept {
                    if *priority == RenamePriority::Heuristic {
                        surviving_module_heuristics.insert((from.0.to_string(), to.to_string()));
                    }
                }
            }
            if !kept.is_empty() {
                sealed.insert(scope, kept);
            }
        }
        Ok(SealedRenames { by_scope: sealed })
    }
}

/// Target validation for `Chunk` / `Module` / `EntryPublicExports`
/// scopes. Explicit intents replay the pre-ledger application-site
/// checks (hard errors, same messages); import-induced intents assert
/// the mint invariant; heuristic intents (free-source aliases) get the
/// [`merge_module_renames`] target-collision rule — their occupancy is
/// deliberately NOT checked here, matching pre-ledger behavior (they
/// apply function-locally; the subtree check runs on their
/// `Function`-scope copies).
fn validate_body_scope(
    scope: &RenameScope,
    label: &str,
    root: &BTreeSet<String>,
    nested: &BTreeSet<String>,
    captured: &BTreeSet<(String, String)>,
    mut winners: BTreeMap<Id, (Atom, RenamePriority)>,
) -> Result<BTreeMap<Id, (Atom, RenamePriority)>> {
    let chunk_style = matches!(scope, RenameScope::Chunk);
    // Sources of explicit renames vacate their root-level slot: a target
    // equal to another explicit rename's source is allowed past the
    // root-collision check (the binding is being renamed away) — but
    // only at the root level; a same-named nested binding stays
    // occupied. Chunk-style validation still reports such targets as
    // duplicates (the pre-ledger loop's growing occupied set already
    // held every root name), while module-style validation has no
    // duplicate-against-root rule, so chain/swap renames are allowed.
    let vacated: BTreeSet<String> = winners
        .iter()
        .filter(|(_, (_, priority))| *priority == RenamePriority::Explicit)
        .map(|(from, _)| from.0.to_string())
        .collect();
    let mut errors = Vec::new();
    // Chunk-style: grows with accepted targets so an earlier accepted
    // target occupies the name for later entries (sorted-by-source
    // order, matching the pre-ledger loop). Module-style: tracks rename
    // targets only, so two explicit renames sharing one target surface
    // (plan building already rejects duplicate export names; this is the
    // seal-side guarantee).
    let mut taken: BTreeSet<String> = if chunk_style {
        root.iter().cloned().collect()
    } else {
        BTreeSet::new()
    };
    for (from, (to, priority)) in &winners {
        if *priority != RenamePriority::Explicit {
            continue;
        }
        let from = from.0.as_ref();
        let to = to.as_ref();
        if chunk_style && !is_valid_js_identifier(to) {
            errors.push(format!(
                "chunk_renames target {to} for binding {from} is not a valid JS identifier",
            ));
            continue;
        }
        let occupied_for_collision = if chunk_style { &taken } else { root };
        if to != from && occupied_for_collision.contains(to) && !vacated.contains(to) {
            errors.push(if chunk_style {
                format!(
                    "chunk_renames target {to} for binding {from} collides with an existing top-level local",
                )
            } else {
                format!(
                    "rename of binding {from} to {to} collides with another top-level binding in the module body",
                )
            });
            continue;
        }
        if !taken.insert(to.to_string()) && to != from {
            errors.push(if chunk_style {
                format!(
                    "chunk_renames target {to} for binding {from} duplicates an earlier rename target",
                )
            } else {
                format!(
                    "rename of binding {from} to {to} duplicates another rename target in the module",
                )
            });
            continue;
        }
    }
    if !errors.is_empty() {
        if chunk_style {
            bail!("invalid chunk_renames spec:\n  - {}", errors.join("\n  - "));
        }
        bail!(
            "invalid renames for module {label}:\n  - {}",
            errors.join("\n  - "),
        );
    }
    // Capture facts the caller's rename walk observed: applying these
    // renames would make a reference resolve to a nested binding of the
    // target name. The pre-seal walk's mutation is discarded with the
    // whole run by this bail.
    if !captured.is_empty() {
        if chunk_style {
            bail!(
                "chunk_renames for chunk {label} would be captured by a nested binding: {captured:?}",
            );
        }
        bail!("renames for module {label} would be captured by a nested binding: {captured:?}",);
    }
    for (from, (to, priority)) in &winners {
        if *priority == RenamePriority::ImportInduced
            && (root.contains(to.as_ref()) || nested.contains(to.as_ref()))
        {
            bail!(
                "internal invariant violation: ledger-minted rename target {to} for binding {} collides with an occupied name in {scope}; minting must claim from the scope's seeded taken set",
                from.0,
            );
        }
    }
    // Heuristic (free-source) target collisions: drop losers silently.
    let explicit_by_name: BTreeMap<String, String> = winners
        .iter()
        .filter(|(_, (_, priority))| *priority != RenamePriority::Heuristic)
        .map(|(from, (to, _))| (from.0.to_string(), to.to_string()))
        .collect();
    let heuristic_by_name: BTreeMap<String, String> = winners
        .iter()
        .filter(|(_, (_, priority))| *priority == RenamePriority::Heuristic)
        .map(|(from, (to, _))| (from.0.to_string(), to.to_string()))
        .collect();
    if !heuristic_by_name.is_empty() {
        let merged = merge_module_renames(explicit_by_name, heuristic_by_name);
        winners.retain(|from, (to, priority)| {
            *priority != RenamePriority::Heuristic
                || merged.get(from.0.as_ref()).map(String::as_str) == Some(to.as_ref())
        });
    }
    Ok(winners)
}

/// Target validation for `Function` scopes, replaying the pre-ledger
/// per-scope heuristic rules. An intent whose source the subtree binds is
/// a bound-source rename (`validated_bound` rules: target unique among
/// the scope's bound-source targets, unreserved, absent from the
/// subtree's mentions, not another bound source). An intent whose source
/// the subtree does NOT bind is a free-source alias copy: its target must
/// not be bound in the subtree and its `(from, to)` pair must have
/// survived Module-scope validation. Failures drop silently
/// (over-suppression is acceptable for heuristics).
fn validate_function_scope(
    bound: &BTreeSet<String>,
    mentions: &BTreeSet<String>,
    reserved: &BTreeSet<String>,
    surviving_module_heuristics: &BTreeSet<(String, String)>,
    mut winners: BTreeMap<Id, (Atom, RenamePriority)>,
) -> BTreeMap<Id, (Atom, RenamePriority)> {
    let bound_sources: BTreeSet<String> = winners
        .keys()
        .map(|from| from.0.to_string())
        .filter(|sym| bound.contains(sym))
        .collect();
    let mut bound_target_counts = BTreeMap::<String, usize>::new();
    for (from, (to, _)) in &winners {
        if bound.contains(from.0.as_ref()) {
            *bound_target_counts.entry(to.to_string()).or_default() += 1;
        }
    }
    winners.retain(|from, (to, priority)| {
        if *priority != RenamePriority::Heuristic {
            return true;
        }
        let to = to.as_ref();
        if bound.contains(from.0.as_ref()) {
            bound_target_counts.get(to).copied() == Some(1)
                && !reserved.contains(to)
                && !mentions.contains(to)
                && !bound_sources.contains(to)
        } else {
            !bound.contains(to)
                && surviving_module_heuristics.contains(&(from.0.to_string(), to.to_string()))
        }
    });
    winners
}

/// Merge `heuristic` into `explicit`, dropping any heuristic mapping
/// whose source `explicit` already renames, whose target `explicit`
/// claims, or whose target is shared with another effective heuristic
/// source. Two sources renamed onto the same target would collapse
/// distinct bindings into a duplicate decl as soon as both happen to live
/// in the same scope. This is the module-level target-collision rule
/// seal applies to free-source aliases; `naturalize_module_body` calls it
/// directly for the derive-phase preview (`free_allowed`) on the scratch
/// clone (see the module doc's "Derive clone" section for why the clone
/// is the fact source).
pub fn merge_module_renames(
    mut explicit: BTreeMap<String, String>,
    heuristic: BTreeMap<String, String>,
) -> BTreeMap<String, String> {
    // Only effective heuristic mappings (sources not already renamed by
    // `explicit`) contribute to the collision count. Counting skipped
    // entries inflates counts[target] and can drop unrelated heuristic
    // mappings that have only one effective claimant.
    let mut counts = BTreeMap::<String, usize>::new();
    for target in explicit.values() {
        *counts.entry(target.clone()).or_default() += 1;
    }
    for (source, target) in &heuristic {
        if explicit.contains_key(source) {
            continue;
        }
        *counts.entry(target.clone()).or_default() += 1;
    }
    for (source, target) in heuristic {
        if explicit.contains_key(&source) {
            continue;
        }
        if counts.get(&target).copied().unwrap_or(0) > 1 {
            continue;
        }
        explicit.insert(source, target);
    }
    explicit
}

/// The validated, read-only output of [`RenameLedger::seal`]: per scope,
/// the final `from → to` mapping (with the winning priority retained so
/// origin-split queries can separate plan-driven renames from
/// heuristics). Queries reproduce exactly the maps the pre-ledger code
/// built so the existing application sites consume them unchanged.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct SealedRenames {
    by_scope: BTreeMap<RenameScope, BTreeMap<Id, (Atom, RenamePriority)>>,
}

impl SealedRenames {
    /// Chunk-scope renames projected to bare names — the map
    /// `LowerChunkSpecFacts::chunk_renames` consumes (pre-ledger:
    /// `collect_chunk_renames`'s output).
    pub fn chunk_renames_by_name(&self) -> HashMap<String, String> {
        self.scope_renames_by_name(&RenameScope::Chunk)
            .into_iter()
            .collect()
    }

    /// One module's renames projected to bare names, sorted — plan-driven
    /// and surviving free-source heuristics merged (pre-ledger: the
    /// `NaturalizedRenames::merged` map).
    pub fn module_renames_by_name(&self, module: ModuleId) -> BTreeMap<String, String> {
        self.scope_renames_by_name(&RenameScope::Module(module))
    }

    /// One module's *explicit* (plan-driven) renames only — the map
    /// export locals and binding-comment keys remap through (heuristic
    /// entries are scope-local and never rename a top-level declaration).
    pub fn module_explicit_renames_by_name(&self, module: ModuleId) -> BTreeMap<String, String> {
        self.scope_renames_at_priority(&RenameScope::Module(module), RenamePriority::Explicit)
    }

    /// Typed per-scope view (tests; the production executors consume the
    /// by-name projection — see the module doc's "Hygiene boundary").
    pub fn scope_renames(&self, scope: &RenameScope) -> Option<BTreeMap<Id, Atom>> {
        self.by_scope.get(scope).map(|renames| {
            renames
                .iter()
                .map(|(from, (to, _))| (from.clone(), to.clone()))
                .collect()
        })
    }

    /// Project one scope's sealed renames onto bare syms for the
    /// string-keyed application visitors. Lossy iff two hygiene contexts
    /// share a sym within one scope — the current contributors resolve
    /// every `from` through one context per scope (chunk-top-level for
    /// `Chunk`/`Module`/`EntryPublicExports`, the string-era empty context
    /// for `Function`), so that cannot happen; assert rather than silently
    /// merge if it ever does.
    pub fn scope_renames_by_name(&self, scope: &RenameScope) -> BTreeMap<String, String> {
        self.scope_renames_by_name_filtered(scope, |_| true)
    }

    fn scope_renames_at_priority(
        &self,
        scope: &RenameScope,
        priority: RenamePriority,
    ) -> BTreeMap<String, String> {
        self.scope_renames_by_name_filtered(scope, |p| p == priority)
    }

    fn scope_renames_by_name_filtered(
        &self,
        scope: &RenameScope,
        keep: impl Fn(RenamePriority) -> bool,
    ) -> BTreeMap<String, String> {
        let Some(renames) = self.by_scope.get(scope) else {
            return BTreeMap::new();
        };
        let mut by_name = BTreeMap::new();
        for (from, (to, priority)) in renames {
            if !keep(*priority) {
                continue;
            }
            let previous = by_name.insert(from.0.to_string(), to.to_string());
            assert!(
                previous.is_none(),
                "two hygiene contexts share sym `{}` in {scope}; \
                 the string-keyed application boundary cannot express this",
                from.0,
            );
        }
        by_name
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use swc_common::SyntaxContext;

    fn id(sym: &str) -> Id {
        (Atom::from(sym), SyntaxContext::empty())
    }

    fn intent(scope: RenameScope, from: &str, to: &str, origin: RenameOrigin) -> RenameIntent {
        RenameIntent {
            scope,
            from: id(from),
            to: Atom::from(to),
            origin,
        }
    }

    fn names(names: &[&str]) -> BTreeSet<String> {
        names.iter().map(|n| n.to_string()).collect()
    }

    fn body_occupancy(scope: RenameScope, root: &[&str], nested: &[&str]) -> SealValidation {
        body_occupancy_with_captures(scope, root, nested, &[])
    }

    fn body_occupancy_with_captures(
        scope: RenameScope,
        root: &[&str],
        nested: &[&str],
        captured: &[(&str, &str)],
    ) -> SealValidation {
        SealValidation {
            occupancy: BTreeMap::from([(
                scope,
                ScopeOccupancy::Body {
                    label: "spec_x".to_string(),
                    root: names(root),
                    nested: names(nested),
                    captured: captured
                        .iter()
                        .map(|(from, to)| (from.to_string(), to.to_string()))
                        .collect(),
                },
            )]),
            reserved: BTreeSet::new(),
        }
    }

    const A: RenameOrigin = RenameOrigin::Explicit {
        contributor: "contributor_a",
    };
    const B: RenameOrigin = RenameOrigin::Explicit {
        contributor: "contributor_b",
    };
    const HEURISTIC: RenameOrigin = RenameOrigin::Heuristic {
        contributor: "scope-local heuristic naturalizer",
    };
    const FREE: RenameOrigin = RenameOrigin::Heuristic {
        contributor: "return-object alias (free source)",
    };
    const MINT: RenameOrigin = RenameOrigin::ImportInduced {
        contributor: "entry import-local disambiguation",
    };

    #[test]
    fn same_priority_conflict_errors_naming_both_contributors() {
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(RenameScope::Chunk, "a", "first", A));
        ledger.submit(intent(RenameScope::Chunk, "a", "second", B));
        let message = ledger
            .seal(&SealValidation::default())
            .unwrap_err()
            .to_string();
        assert!(message.contains("contributor_a"), "{message}");
        assert!(message.contains("contributor_b"), "{message}");
        assert!(message.contains("`first`"), "{message}");
        assert!(message.contains("`second`"), "{message}");
    }

    #[test]
    fn reports_every_conflict_in_one_error() {
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(RenameScope::Chunk, "a", "first", A));
        ledger.submit(intent(RenameScope::Chunk, "a", "second", B));
        ledger.submit(intent(RenameScope::Chunk, "b", "third", A));
        ledger.submit(intent(RenameScope::Chunk, "b", "fourth", B));
        let message = ledger
            .seal(&SealValidation::default())
            .unwrap_err()
            .to_string();
        assert!(message.contains("binding `a`"), "{message}");
        assert!(message.contains("binding `b`"), "{message}");
    }

    #[test]
    fn identical_duplicate_intents_collapse() {
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(RenameScope::Chunk, "a", "readable", A));
        ledger.submit(intent(RenameScope::Chunk, "a", "readable", B));
        let sealed = ledger.seal(&SealValidation::default()).unwrap();
        assert_eq!(
            sealed.chunk_renames_by_name(),
            HashMap::from([("a".to_string(), "readable".to_string())]),
        );
    }

    #[test]
    fn higher_priority_wins_over_disagreeing_lower_priority() {
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(
            RenameScope::Chunk,
            "a",
            "heuristic_name",
            RenameOrigin::Heuristic {
                contributor: "alias_inference",
            },
        ));
        ledger.submit(intent(RenameScope::Chunk, "a", "explicit_name", A));
        let sealed = ledger.seal(&SealValidation::default()).unwrap();
        assert_eq!(
            sealed.chunk_renames_by_name(),
            HashMap::from([("a".to_string(), "explicit_name".to_string())]),
        );
    }

    #[test]
    fn cross_scope_intents_do_not_leak_or_conflict() {
        let function_scope = RenameScope::Function(FunctionScopeId { lo: 10, hi: 20 });
        let module_scope = RenameScope::Module(ModuleId::logical(0));
        let mut ledger = RenameLedger::default();
        // Same `from`, different targets — fine across scopes.
        ledger.submit(intent(
            function_scope,
            "e",
            "value",
            RenameOrigin::Heuristic {
                contributor: "destructure_unpack",
            },
        ));
        ledger.submit(intent(module_scope, "e", "registry", A));
        let sealed = ledger.seal(&SealValidation::default()).unwrap();
        assert_eq!(
            sealed.module_renames_by_name(ModuleId::logical(0)),
            BTreeMap::from([("e".to_string(), "registry".to_string())]),
        );
        assert!(sealed.chunk_renames_by_name().is_empty());
        assert_eq!(
            sealed.scope_renames(&function_scope),
            Some(BTreeMap::from([(id("e"), Atom::from("value"))])),
        );
        assert_eq!(sealed.scope_renames(&RenameScope::Chunk), None);
    }

    #[test]
    fn module_queries_are_isolated_per_module() {
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(
            RenameScope::Module(ModuleId::logical(0)),
            "a",
            "x",
            A,
        ));
        ledger.submit(intent(
            RenameScope::Module(ModuleId::logical(1)),
            "b",
            "y",
            A,
        ));
        let sealed = ledger.seal(&SealValidation::default()).unwrap();
        assert_eq!(
            sealed.module_renames_by_name(ModuleId::logical(0)),
            BTreeMap::from([("a".to_string(), "x".to_string())]),
        );
        assert_eq!(
            sealed.module_renames_by_name(ModuleId::logical(1)),
            BTreeMap::from([("b".to_string(), "y".to_string())]),
        );
        assert!(
            sealed
                .module_renames_by_name(ModuleId::logical(2))
                .is_empty()
        );
    }

    #[test]
    fn submission_order_does_not_change_seal_output() {
        let intents = [
            intent(RenameScope::Chunk, "z", "zulu", A),
            intent(RenameScope::Chunk, "a", "alpha", B),
            intent(RenameScope::Module(ModuleId::logical(3)), "m", "mike", A),
            intent(
                RenameScope::Function(FunctionScopeId { lo: 1, hi: 2 }),
                "f",
                "foxtrot",
                RenameOrigin::Heuristic {
                    contributor: "alias_inference",
                },
            ),
        ];
        let mut forward = RenameLedger::default();
        for i in &intents {
            forward.submit(i.clone());
        }
        let mut reverse = RenameLedger::default();
        for i in intents.iter().rev() {
            reverse.submit(i.clone());
        }
        assert_eq!(
            forward.seal(&SealValidation::default()).unwrap(),
            reverse.seal(&SealValidation::default()).unwrap(),
        );
    }

    #[test]
    fn entry_public_exports_scope_is_isolated_from_chunk_scope() {
        // The export-name namespace is separate from local-binding
        // renames: one binding may simultaneously carry a Chunk-scope
        // local rename and an EntryPublicExports-scope public-name
        // allocation without conflicting.
        let mint = RenameOrigin::ImportInduced {
            contributor: "auto-grown residual export",
        };
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(RenameScope::Chunk, "a", "readable", A));
        ledger.submit(intent(RenameScope::EntryPublicExports, "a", "a$1", mint));
        let sealed = ledger.seal(&SealValidation::default()).unwrap();
        assert_eq!(
            sealed.scope_renames_by_name(&RenameScope::Chunk),
            BTreeMap::from([("a".to_string(), "readable".to_string())]),
        );
        assert_eq!(
            sealed.scope_renames_by_name(&RenameScope::EntryPublicExports),
            BTreeMap::from([("a".to_string(), "a$1".to_string())]),
        );
    }

    #[test]
    fn import_induced_conflict_is_a_hard_error_naming_both_minters() {
        let cross = RenameOrigin::ImportInduced {
            contributor: "cross-module import-local disambiguation",
        };
        let residual = RenameOrigin::ImportInduced {
            contributor: "residual-entry import-local disambiguation",
        };
        let module = RenameScope::Module(ModuleId::logical(0));
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(module, "x", "x$1", cross));
        ledger.submit(intent(module, "x", "x$2", residual));
        let message = ledger
            .seal(&SealValidation::default())
            .unwrap_err()
            .to_string();
        assert!(
            message.contains("cross-module import-local disambiguation"),
            "{message}"
        );
        assert!(
            message.contains("residual-entry import-local disambiguation"),
            "{message}"
        );
    }

    #[test]
    fn function_scope_projection_returns_per_scope_string_maps() {
        let outer = RenameScope::Function(FunctionScopeId { lo: 1, hi: 100 });
        let inner = RenameScope::Function(FunctionScopeId { lo: 10, hi: 20 });
        let mut ledger = RenameLedger::default();
        // Sibling/nested scopes reusing one minified spelling with
        // different targets are independent renames (#2045) — no conflict.
        ledger.submit(intent(outer, "e", "value", HEURISTIC));
        ledger.submit(intent(inner, "e", "registry", HEURISTIC));
        let sealed = ledger.seal(&SealValidation::default()).unwrap();
        assert_eq!(
            sealed.scope_renames_by_name(&outer),
            BTreeMap::from([("e".to_string(), "value".to_string())]),
        );
        assert_eq!(
            sealed.scope_renames_by_name(&inner),
            BTreeMap::from([("e".to_string(), "registry".to_string())]),
        );
        assert!(
            sealed
                .scope_renames_by_name(&RenameScope::Function(FunctionScopeId { lo: 2, hi: 3 }))
                .is_empty()
        );
    }

    #[test]
    fn hygiene_distinct_ids_are_distinct_keys() {
        // Two bindings spelled the same but carrying different
        // SyntaxContexts are different ledger keys — no conflict.
        let other_ctxt = (Atom::from("a"), SyntaxContext::from_u32(7));
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(RenameScope::Chunk, "a", "first", A));
        ledger.submit(RenameIntent {
            scope: RenameScope::Module(ModuleId::logical(0)),
            from: other_ctxt.clone(),
            to: Atom::from("second"),
            origin: B,
        });
        let sealed = ledger.seal(&SealValidation::default()).unwrap();
        assert_eq!(
            sealed.scope_renames(&RenameScope::Module(ModuleId::logical(0))),
            Some(BTreeMap::from([(other_ctxt, Atom::from("second"))])),
        );
    }

    // --- minting ---

    #[test]
    fn mint_returns_base_when_untaken_and_suffixes_past_collisions() {
        let mut ledger = RenameLedger::default();
        ledger.seed_taken(RenameScope::Chunk, ["taken".to_string()]);
        assert_eq!(ledger.mint(RenameScope::Chunk, "fresh"), "fresh");
        // The minted name is claimed: a second request suffixes.
        assert_eq!(ledger.mint(RenameScope::Chunk, "fresh"), "fresh$1");
        assert_eq!(ledger.mint(RenameScope::Chunk, "fresh"), "fresh$2");
        assert_eq!(ledger.mint(RenameScope::Chunk, "taken"), "taken$1");
    }

    #[test]
    fn mint_never_offers_a_reserved_word_verbatim() {
        let mut ledger = RenameLedger::default();
        assert_eq!(ledger.mint(RenameScope::Chunk, "default"), "default$1");
    }

    #[test]
    fn mint_taken_sets_are_per_scope() {
        let mut ledger = RenameLedger::default();
        ledger.seed_taken(RenameScope::Chunk, ["name".to_string()]);
        assert_eq!(ledger.mint(RenameScope::Chunk, "name"), "name$1");
        assert_eq!(
            ledger.mint(RenameScope::Module(ModuleId::logical(0)), "name"),
            "name",
        );
    }

    #[test]
    fn claim_reserves_a_name_for_later_mints() {
        let mut ledger = RenameLedger::default();
        assert!(ledger.claim(RenameScope::Chunk, "readable"));
        assert!(!ledger.claim(RenameScope::Chunk, "readable"));
        assert_eq!(ledger.mint(RenameScope::Chunk, "readable"), "readable$1");
    }

    // --- explicit occupancy validation (hard errors) ---

    #[test]
    fn chunk_explicit_target_colliding_with_root_binding_is_a_hard_error() {
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(RenameScope::Chunk, "a", "delta", A));
        let message = ledger
            .seal(&body_occupancy(RenameScope::Chunk, &["a", "delta"], &[]))
            .unwrap_err()
            .to_string();
        assert!(message.contains("invalid chunk_renames spec"), "{message}");
        assert!(
            message.contains("collides with an existing top-level local"),
            "{message}"
        );
    }

    #[test]
    fn chunk_explicit_violations_surface_together() {
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(RenameScope::Chunk, "alpha", "1-bad-ident", A));
        ledger.submit(intent(RenameScope::Chunk, "bravo", "delta", A));
        ledger.submit(intent(RenameScope::Chunk, "charlie", "shared", A));
        ledger.submit(intent(RenameScope::Chunk, "delta", "shared", A));
        let message = ledger
            .seal(&body_occupancy(
                RenameScope::Chunk,
                &["alpha", "bravo", "charlie", "delta"],
                &[],
            ))
            .unwrap_err()
            .to_string();
        assert!(message.contains("not a valid JS identifier"), "{message}");
        assert!(
            message.contains("collides with an existing top-level local"),
            "{message}"
        );
        assert!(
            message.contains("duplicates an earlier rename target"),
            "{message}"
        );
    }

    #[test]
    fn chunk_chain_rename_onto_vacated_name_reports_duplicate() {
        // Chain renames a→b, b→c at Chunk scope: `b`'s vacated root slot
        // routes the violation past the "collides" branch, but the
        // growing occupied set (which holds every root name) still
        // reports it — the pre-ledger loop's exact semantics.
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(RenameScope::Chunk, "a", "b", A));
        ledger.submit(intent(RenameScope::Chunk, "b", "c", A));
        let message = ledger
            .seal(&body_occupancy(RenameScope::Chunk, &["a", "b"], &[]))
            .unwrap_err()
            .to_string();
        assert!(
            message.contains("duplicates an earlier rename target"),
            "{message}"
        );
    }

    #[test]
    fn module_swap_renames_are_allowed_via_vacating() {
        // Module-scope explicit renames may swap two bindings' names:
        // each target's root slot is vacated by the other rename, and
        // module-style validation has no duplicate-against-root rule.
        let module = RenameScope::Module(ModuleId::logical(0));
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(module, "a", "b", A));
        ledger.submit(intent(module, "b", "a", A));
        let sealed = ledger
            .seal(&body_occupancy(module, &["a", "b"], &[]))
            .unwrap();
        assert_eq!(
            sealed.module_renames_by_name(ModuleId::logical(0)),
            BTreeMap::from([
                ("a".to_string(), "b".to_string()),
                ("b".to_string(), "a".to_string()),
            ]),
        );
    }

    #[test]
    fn observed_capture_facts_are_a_hard_error() {
        // The caller's rename walk reports `(source, target)` pairs the
        // scope stack withheld; seal rejects with the pre-ledger message.
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(RenameScope::Chunk, "a", "b", A));
        let message = ledger
            .seal(&body_occupancy_with_captures(
                RenameScope::Chunk,
                &["a", "f"],
                &["b"],
                &[("a", "b")],
            ))
            .unwrap_err()
            .to_string();
        assert!(
            message.contains("captured by a nested binding"),
            "{message}"
        );
    }

    #[test]
    fn nested_bound_target_without_observed_capture_is_allowed() {
        // Reference-precision: a target bound only in a nested scope
        // where the source is shadowed (or never referenced) does not
        // capture — the rename walk reports no pair, so seal accepts.
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(RenameScope::Chunk, "a", "b", A));
        let sealed = ledger
            .seal(&body_occupancy(RenameScope::Chunk, &["a", "f"], &["b"]))
            .unwrap();
        assert_eq!(
            sealed.chunk_renames_by_name(),
            HashMap::from([("a".to_string(), "b".to_string())]),
        );
    }

    #[test]
    fn module_explicit_target_colliding_with_root_binding_is_a_hard_error() {
        let module = RenameScope::Module(ModuleId::logical(0));
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(module, "a", "readable", A));
        let message = ledger
            .seal(&body_occupancy(module, &["a", "readable"], &[]))
            .unwrap_err()
            .to_string();
        assert!(
            message.contains("invalid renames for module spec_x"),
            "{message}"
        );
        assert!(
            message.contains(
                "rename of binding a to readable collides with another top-level binding"
            ),
            "{message}"
        );
    }

    #[test]
    fn minted_target_colliding_with_occupancy_is_an_invariant_error() {
        // Mints come from the ledger's own taken set; a collision means
        // the caller seeded the wrong occupancy — an internal bug, not a
        // spec error.
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(RenameScope::Chunk, "x", "x$1", MINT));
        let message = ledger
            .seal(&body_occupancy(RenameScope::Chunk, &["x", "x$1"], &[]))
            .unwrap_err()
            .to_string();
        assert!(
            message.contains("internal invariant violation"),
            "{message}"
        );
    }

    // --- heuristic drop policies ---

    #[test]
    fn module_heuristics_sharing_a_target_are_both_dropped() {
        let module = RenameScope::Module(ModuleId::logical(0));
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(module, "a", "shared", FREE));
        ledger.submit(intent(module, "b", "shared", FREE));
        ledger.submit(intent(module, "c", "unique", FREE));
        let sealed = ledger.seal(&body_occupancy(module, &[], &[])).unwrap();
        assert_eq!(
            sealed.module_renames_by_name(ModuleId::logical(0)),
            BTreeMap::from([("c".to_string(), "unique".to_string())]),
        );
    }

    #[test]
    fn module_heuristic_targeting_an_explicit_target_is_dropped() {
        let module = RenameScope::Module(ModuleId::logical(0));
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(module, "a", "winner", A));
        ledger.submit(intent(module, "b", "winner", FREE));
        let sealed = ledger.seal(&body_occupancy(module, &["a"], &[])).unwrap();
        assert_eq!(
            sealed.module_renames_by_name(ModuleId::logical(0)),
            BTreeMap::from([("a".to_string(), "winner".to_string())]),
        );
        assert_eq!(
            sealed.module_explicit_renames_by_name(ModuleId::logical(0)),
            BTreeMap::from([("a".to_string(), "winner".to_string())]),
        );
    }

    #[test]
    fn module_explicit_query_excludes_heuristic_survivors() {
        let module = RenameScope::Module(ModuleId::logical(0));
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(module, "a", "plan_name", A));
        ledger.submit(intent(module, "t", "options", FREE));
        let sealed = ledger.seal(&body_occupancy(module, &["a"], &[])).unwrap();
        assert_eq!(
            sealed.module_renames_by_name(ModuleId::logical(0)),
            BTreeMap::from([
                ("a".to_string(), "plan_name".to_string()),
                ("t".to_string(), "options".to_string()),
            ]),
        );
        assert_eq!(
            sealed.module_explicit_renames_by_name(ModuleId::logical(0)),
            BTreeMap::from([("a".to_string(), "plan_name".to_string())]),
        );
    }

    // --- Function-scope occupancy (validated_bound replay) ---

    fn function_validation(
        scope: RenameScope,
        bound: &[&str],
        mentions: &[&str],
        reserved: &[&str],
    ) -> SealValidation {
        SealValidation {
            occupancy: BTreeMap::from([(
                scope,
                ScopeOccupancy::Subtree {
                    bound: names(bound),
                    mentions: names(mentions),
                },
            )]),
            reserved: names(reserved),
        }
    }

    #[test]
    fn bound_source_heuristic_dropped_when_target_mentioned_in_subtree() {
        let scope = RenameScope::Function(FunctionScopeId { lo: 1, hi: 9 });
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(scope, "e", "value", HEURISTIC));
        let sealed = ledger
            .seal(&function_validation(scope, &["e"], &["e", "value"], &[]))
            .unwrap();
        assert!(sealed.scope_renames_by_name(&scope).is_empty());
    }

    #[test]
    fn bound_source_heuristic_dropped_when_target_reserved_module_wide() {
        let scope = RenameScope::Function(FunctionScopeId { lo: 1, hi: 9 });
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(scope, "e", "options", HEURISTIC));
        let sealed = ledger
            .seal(&function_validation(scope, &["e"], &["e"], &["options"]))
            .unwrap();
        assert!(sealed.scope_renames_by_name(&scope).is_empty());
    }

    #[test]
    fn bound_source_heuristics_sharing_a_target_are_both_dropped() {
        let scope = RenameScope::Function(FunctionScopeId { lo: 1, hi: 9 });
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(scope, "e", "value", HEURISTIC));
        ledger.submit(intent(scope, "n", "value", HEURISTIC));
        ledger.submit(intent(scope, "k", "kept", HEURISTIC));
        let sealed = ledger
            .seal(&function_validation(
                scope,
                &["e", "n", "k"],
                &["e", "n", "k"],
                &[],
            ))
            .unwrap();
        assert_eq!(
            sealed.scope_renames_by_name(&scope),
            BTreeMap::from([("k".to_string(), "kept".to_string())]),
        );
    }

    #[test]
    fn bound_source_heuristic_dropped_when_target_is_another_source() {
        let scope = RenameScope::Function(FunctionScopeId { lo: 1, hi: 9 });
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(scope, "e", "n", HEURISTIC));
        ledger.submit(intent(scope, "n", "fresh", HEURISTIC));
        let sealed = ledger
            .seal(&function_validation(scope, &["e", "n"], &["e", "n"], &[]))
            .unwrap();
        assert_eq!(
            sealed.scope_renames_by_name(&scope),
            BTreeMap::from([("n".to_string(), "fresh".to_string())]),
        );
    }

    #[test]
    fn free_alias_copy_dropped_when_target_bound_in_subtree() {
        // Free-source aliases (source NOT bound in the subtree) are
        // exempt from the mentions rule but must not target a name the
        // subtree binds.
        let module = RenameScope::Module(ModuleId::logical(0));
        let scope = RenameScope::Function(FunctionScopeId { lo: 1, hi: 9 });
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(module, "t", "options", FREE));
        ledger.submit(intent(scope, "t", "options", FREE));
        let mut validation = function_validation(scope, &["options"], &["t", "options"], &[]);
        validation.occupancy.insert(
            module,
            ScopeOccupancy::Body {
                label: "spec_x".to_string(),
                root: BTreeSet::new(),
                nested: BTreeSet::new(),
                captured: BTreeSet::new(),
            },
        );
        let sealed = ledger.seal(&validation).unwrap();
        // The Module-scope intent survives (merged-map bridge); the
        // Function-scope application copy is suppressed.
        assert_eq!(
            sealed.module_renames_by_name(ModuleId::logical(0)),
            BTreeMap::from([("t".to_string(), "options".to_string())]),
        );
        assert!(sealed.scope_renames_by_name(&scope).is_empty());
    }

    #[test]
    fn free_alias_copy_survives_when_target_only_mentioned() {
        let module = RenameScope::Module(ModuleId::logical(0));
        let scope = RenameScope::Function(FunctionScopeId { lo: 1, hi: 9 });
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(module, "t", "options", FREE));
        ledger.submit(intent(scope, "t", "options", FREE));
        let mut validation = function_validation(scope, &[], &["t", "options"], &[]);
        validation.occupancy.insert(
            module,
            ScopeOccupancy::Body {
                label: "spec_x".to_string(),
                root: BTreeSet::new(),
                nested: BTreeSet::new(),
                captured: BTreeSet::new(),
            },
        );
        let sealed = ledger.seal(&validation).unwrap();
        assert_eq!(
            sealed.scope_renames_by_name(&scope),
            BTreeMap::from([("t".to_string(), "options".to_string())]),
        );
    }

    #[test]
    fn free_alias_copy_dropped_when_module_intent_lost_collision() {
        // Two deriving functions free-alias different sources onto one
        // target: both Module-scope intents drop (target collision), so
        // both Function-scope application copies must drop too.
        let module = RenameScope::Module(ModuleId::logical(0));
        let f1 = RenameScope::Function(FunctionScopeId { lo: 1, hi: 9 });
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(module, "t", "options", FREE));
        ledger.submit(intent(module, "u", "options", FREE));
        ledger.submit(intent(f1, "t", "options", FREE));
        let mut validation = function_validation(f1, &[], &["t"], &[]);
        validation.occupancy.insert(
            module,
            ScopeOccupancy::Body {
                label: "spec_x".to_string(),
                root: BTreeSet::new(),
                nested: BTreeSet::new(),
                captured: BTreeSet::new(),
            },
        );
        let sealed = ledger.seal(&validation).unwrap();
        assert!(
            sealed
                .module_renames_by_name(ModuleId::logical(0))
                .is_empty()
        );
        assert!(sealed.scope_renames_by_name(&f1).is_empty());
    }

    #[test]
    fn scopes_without_occupancy_skip_target_validation() {
        // The chunk-level explicit ledger seals before the post-split
        // bodies exist; its intents are occupancy-validated downstream.
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(RenameScope::Chunk, "a", "delta", A));
        let sealed = ledger.seal(&SealValidation::default()).unwrap();
        assert_eq!(
            sealed.chunk_renames_by_name(),
            HashMap::from([("a".to_string(), "delta".to_string())]),
        );
    }
}
