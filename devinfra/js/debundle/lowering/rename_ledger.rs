//! `RenameLedger` — the intent buffer of the collect → validate →
//! execute-once rename pipeline (sanitization program Track B; TODO.md
//! "Rename pipeline: collect → validate → execute _once_").
//!
//! Rename contributors submit [`RenameIntent`]s instead of building their
//! own maps; [`RenameLedger::seal`] validates the intents and freezes them
//! into [`SealedRenames`], whose queries reproduce exactly the maps the
//! pre-ledger code built so the existing application sites (the
//! string-keyed rename visitors in `visitors.rs`) stay unchanged.
//!
//! Current seal validation: no two same-priority intents may disagree on
//! one `(scope, from)` binding's target — a hard error naming both
//! contributors. A higher-priority intent (explicit > import-induced >
//! heuristic) silently wins over a disagreeing lower-priority one.
//! Target-occupancy / capture validation still happens at the application
//! sites (`naturalize_module_body`'s root-collision pre-check, the
//! visitors' `captured` sets); moving it into seal against scope-accurate
//! occupied sets is PR 3. The single execute pass is PR 4.
//!
//! ## Contract: no structural moves between seal and execute
//!
//! Adopted (decided 2026-06): once a chunk's ledger is sealed, no pass may
//! move declarations between modules — structural moves (entry-body split,
//! residual sweep, rebind folds, mini-factor synthesis) must all happen
//! before collection finishes. In PR 1 the materializer already satisfies
//! the collection side (intents are collected from the finalized
//! `ChunkPlan`); the contract becomes mechanically enforceable when the
//! execute-once pass lands (PR 4) and non-execute passes take `&Module`.
//!
//! ## Hygiene boundary
//!
//! Intents are keyed by hygiene-aware [`Id`] — post-#2042 the chunk AST
//! carries real `SyntaxContext`s and bare-string keys are how rename bugs
//! breed. Contributors that only have a spec string resolve it at the
//! collection point via `top_level_id(name, chunk_top_level_mark)`. The
//! seal output is projected back to bare syms at the query boundary
//! (`*_by_name`) because the post-#2052 application visitors are still
//! string-keyed; the projection asserts that no two hygiene contexts share
//! a sym within one scope. PR 4's executor consumes `Id`s directly and
//! deletes the projection.
//!
//! ## Contributor inventory
//!
//! Converted (submit intents):
//!
//! - Spec `chunk_renames` — `chunk_renames.rs::collect_chunk_renames`
//!   (scope: `Chunk`, origin: `Explicit`).
//! - Plan-driven spec `export_name`s —
//!   `naturalize.rs::collect_plan_export_rename_intents` (scope:
//!   `Module`, origin: `Explicit`).
//!
//! Remaining (PR 2 worklist; each keeps building its own map today):
//!
//! - Heuristic bound-source scope-local renames — `naturalize.rs`:
//!   `collect_naturalization_renames_from_pattern`,
//!   `collect_naturalization_renames_from_constructor`, and root-bound
//!   return-object aliases, derived and applied per function-like node by
//!   `ScopedHeuristicNaturalizer::{visit_mut_function, visit_mut_arrow_expr,
//!   visit_mut_constructor}` (scope: `Function`, origin: `Heuristic`).
//! - Heuristic free-source return-object aliases — `naturalize.rs`:
//!   `collect_free_alias_renames_from_item`, merged module-wide via
//!   `drop_target_collisions` (scope: `Module`, origin: `Heuristic`).
//! - Import-local disambiguation (fresh-local `$N` minting) —
//!   `import_emit.rs`: `disambiguate_import_locals`,
//!   `disambiguate_residual_entry_import_locals`, `mint_unique_name`;
//!   entry-side call site in `lower.rs` (entry-import build seeding
//!   `body_renames`), module-side call sites in
//!   `imports_cross.rs::cross_module_imports_for_plan` and
//!   `imports_cross.rs::residual_entry_imports_for_moved_body` (scope:
//!   `Chunk` / `Module`, origin: `ImportInduced`). Minting becomes a
//!   ledger service in PR 3; decided 2026-06: the `$N` suffix scheme
//!   stays as-is (readability is a later naturalizer concern).
//! - Cross-module rename application to moved bodies — the
//!   `module_import_renames` map accumulated across the two
//!   `imports_cross.rs` helpers above and applied in
//!   `lower.rs::lower_single_plan` (origin: `ImportInduced`).
//! - Collision-resolving public-name minting —
//!   `exports.rs::auto_grown_residual_exports` (suffix-mints a grown
//!   public export past pre-existing public names; origin:
//!   `ImportInduced`).
//!
//! Vendor boundary renames (`vendor/`) run in a separate pipeline stage on
//! different artifacts and are out of this ledger's scope.

use std::collections::{BTreeMap, HashMap};
use std::fmt;

use analysis::ModuleId;
use anyhow::{Result, bail};
use swc_atoms::Atom;
use swc_common::Span;
use swc_ecma_ast::Id;

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
}

impl fmt::Display for RenameScope {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RenameScope::Chunk => write!(f, "chunk scope"),
            RenameScope::Module(ModuleId(index)) => write!(f, "module #{}", index.0),
            RenameScope::Function(span) => write!(f, "function@{}..{}", span.lo, span.hi),
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

/// Accumulates [`RenameIntent`]s during the collect phase; consumed by
/// [`Self::seal`].
#[derive(Debug, Default)]
pub struct RenameLedger {
    intents: Vec<RenameIntent>,
}

impl RenameLedger {
    pub fn submit(&mut self, intent: RenameIntent) {
        self.intents.push(intent);
    }

    /// Validate and freeze the collected intents. Per `(scope, from)`
    /// group, the highest-priority intents must agree on one target —
    /// disagreement at equal priority is a hard error naming every
    /// contributor on each side. Lower-priority disagreement loses
    /// silently. Identical duplicates collapse. Every conflict in the
    /// ledger is reported in one error, not just the first.
    pub fn seal(self) -> Result<SealedRenames> {
        let mut groups: BTreeMap<(RenameScope, Id), Vec<RenameIntent>> = BTreeMap::new();
        for intent in self.intents {
            groups
                .entry((intent.scope, intent.from.clone()))
                .or_default()
                .push(intent);
        }
        let mut by_scope: BTreeMap<RenameScope, BTreeMap<Id, Atom>> = BTreeMap::new();
        let mut conflicts = Vec::new();
        for ((scope, from), intents) in groups {
            let top_priority = intents
                .iter()
                .map(|intent| intent.origin.priority())
                .max()
                .expect("groups hold at least one intent");
            // Distinct targets proposed at the top priority, each with the
            // (sorted, deduped) contributors proposing it.
            let mut by_target: BTreeMap<&Atom, Vec<RenameOrigin>> = BTreeMap::new();
            for intent in &intents {
                if intent.origin.priority() == top_priority {
                    let origins = by_target.entry(&intent.to).or_default();
                    if !origins.contains(&intent.origin) {
                        origins.push(intent.origin);
                    }
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
                .insert(from, (*to).clone());
        }
        if !conflicts.is_empty() {
            bail!(
                "conflicting rename intents:\n  - {}",
                conflicts.join("\n  - "),
            );
        }
        Ok(SealedRenames { by_scope })
    }
}

/// The validated, read-only output of [`RenameLedger::seal`]: per scope,
/// the final `from → to` mapping. Queries reproduce exactly the maps the
/// pre-ledger code built so the existing application sites consume them
/// unchanged.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct SealedRenames {
    by_scope: BTreeMap<RenameScope, BTreeMap<Id, Atom>>,
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

    /// One module's plan-driven renames projected to bare names, sorted —
    /// the `plan_driven` map `naturalize_module_body` consumes
    /// (pre-ledger: derived inline from `plan.bindings`).
    pub fn module_renames_by_name(&self, module: ModuleId) -> BTreeMap<String, String> {
        self.scope_renames_by_name(&RenameScope::Module(module))
    }

    /// Typed per-scope view (tests, the future execute-once pass).
    pub fn scope_renames(&self, scope: &RenameScope) -> Option<&BTreeMap<Id, Atom>> {
        self.by_scope.get(scope)
    }

    /// Project one scope's sealed renames onto bare syms for the
    /// string-keyed application visitors. Lossy iff two hygiene contexts
    /// share a sym within one scope — the current contributors resolve
    /// every `from` through one chunk-top-level context, so that cannot
    /// happen; assert rather than silently merge if it ever does.
    fn scope_renames_by_name(&self, scope: &RenameScope) -> BTreeMap<String, String> {
        let Some(renames) = self.by_scope.get(scope) else {
            return BTreeMap::new();
        };
        let mut by_name = BTreeMap::new();
        for (from, to) in renames {
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

    const A: RenameOrigin = RenameOrigin::Explicit {
        contributor: "contributor_a",
    };
    const B: RenameOrigin = RenameOrigin::Explicit {
        contributor: "contributor_b",
    };

    #[test]
    fn same_priority_conflict_errors_naming_both_contributors() {
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(RenameScope::Chunk, "a", "first", A));
        ledger.submit(intent(RenameScope::Chunk, "a", "second", B));
        let message = ledger.seal().unwrap_err().to_string();
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
        let message = ledger.seal().unwrap_err().to_string();
        assert!(message.contains("binding `a`"), "{message}");
        assert!(message.contains("binding `b`"), "{message}");
    }

    #[test]
    fn identical_duplicate_intents_collapse() {
        let mut ledger = RenameLedger::default();
        ledger.submit(intent(RenameScope::Chunk, "a", "readable", A));
        ledger.submit(intent(RenameScope::Chunk, "a", "readable", B));
        let sealed = ledger.seal().unwrap();
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
        let sealed = ledger.seal().unwrap();
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
        let sealed = ledger.seal().unwrap();
        assert_eq!(
            sealed.module_renames_by_name(ModuleId::logical(0)),
            BTreeMap::from([("e".to_string(), "registry".to_string())]),
        );
        assert!(sealed.chunk_renames_by_name().is_empty());
        assert_eq!(
            sealed.scope_renames(&function_scope),
            Some(&BTreeMap::from([(id("e"), Atom::from("value"))])),
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
        let sealed = ledger.seal().unwrap();
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
        assert_eq!(forward.seal().unwrap(), reverse.seal().unwrap());
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
        let sealed = ledger.seal().unwrap();
        assert_eq!(
            sealed.scope_renames(&RenameScope::Module(ModuleId::logical(0))),
            Some(&BTreeMap::from([(other_ctxt, Atom::from("second"))])),
        );
    }
}
