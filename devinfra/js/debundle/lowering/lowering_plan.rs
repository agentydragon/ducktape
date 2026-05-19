//! Collect-validate-execute buffer for lowering mutations
//! (renames + declaration moves) on a single emitted JS file.
//! Contributors submit [`LoweringOp`]s; [`LoweringPlan::seal`]
//! validates cross-op coherence and returns a [`CheckedPlan`]
//! the execute pass in `lowering_execute.rs` applies in one
//! tree walk. See <plans/debundler_rename_lowering_pipeline.md>
//! for the design.
//!
//! Each instance is per-file, not per-chunk: `lower_chunk`
//! constructs three (residual chunk_renames, entry-body
//! disambig, per-moved-module) because each has a different
//! set of bodies it applies to.

use std::collections::{HashMap, HashSet};

use anyhow::{Result, anyhow};
use swc_atoms::Atom;
use swc_ecma_ast::Id;

use analysis::ModuleId;

use super::util::is_valid_js_identifier;

/// Lexical scope of a rename or query. Forms a tree rooted at
/// [`Scope::Chunk`]; `is_name_taken` walks *up* the chain so an
/// inner-scope rename doesn't shadow a name held by an enclosing
/// scope. Downward shadow detection (chunk-level rename
/// colliding with a function-local binding of the same name) is
/// not modeled — matches today's `disambiguate_import_locals`
/// semantics.
#[derive(Clone, Copy, Debug, Hash, Eq, PartialEq)]
pub enum Scope {
    Chunk,
    Module(ModuleId),
    Function(FunctionScopeId),
}

/// Handle minted by the plan for each function scope. Carries a
/// `parent: Scope` so `is_name_taken` can walk up to the
/// enclosing module/chunk.
#[derive(Clone, Copy, Debug, Hash, Eq, PartialEq)]
pub struct FunctionScopeId(pub usize);

/// Priority lattice over rename contributors. Higher variants
/// win against lower variants under
/// [`SubmitPolicy::SkipIfClaimed`]; same-priority disagreements
/// always error regardless of `SubmitPolicy`. Order is
/// `Heuristic < ImportInduced < Collision < Explicit`.
#[derive(Clone, Copy, Debug, Hash, Eq, PartialEq, Ord, PartialOrd)]
pub enum Priority {
    Heuristic,
    ImportInduced,
    Collision,
    Explicit,
}

/// How a name-producing op (today: [`LoweringOp::Rename`]) wants
/// the plan to pick the final name when its preferred name is
/// taken in the relevant scope. Orthogonal to [`SubmitPolicy`].
#[derive(Clone, Debug)]
pub enum NamePolicy {
    /// Use this exact name; return `Err(ValidationError)` if the
    /// name is already taken. Spec-driven and naturalizer-driven
    /// renames use this — the target name carries semantic meaning.
    Required(Atom),
    /// Prefer this name; if it's taken, fall back to `name_1`,
    /// `name_2`, … until a free name is found. Collision-resolution
    /// renames use this (today's `disambiguate_import_locals_via_plan`
    /// and `disambiguate_residual_entry_import_locals_via_plan`).
    MintOrSuffix(Atom),
}

/// How to handle "(scope, original) already claimed by a previously
/// submitted op". Orthogonal to [`NamePolicy`].
#[derive(Clone, Copy, Debug)]
pub enum SubmitPolicy {
    /// Error on conflict citing both contributors' `reason`s.
    /// Spec-driven ops use this — a conflict is a spec bug.
    Fail,
    /// Drop this op if `(scope, original)` is already claimed at
    /// strictly higher priority. Same-priority disagreements still
    /// error (regardless of policy). Heuristic contributors use
    /// this to defer to the spec.
    SkipIfClaimed,
}

#[derive(Clone, Debug)]
pub enum LoweringOp {
    Rename {
        scope: Scope,
        original: Id,
        name: NamePolicy,
        reason: &'static str,
        priority: Priority,
    },
    MoveBinding {
        id: Id,
        to: ModuleId,
        reason: &'static str,
    },
    // Design also lists `RewriteImportSpecifier`, `AddExport`,
    // `ReorderHoists`; add on first use.
}

#[derive(Clone, Debug)]
pub enum SubmitOutcome {
    Accepted { final_op: LoweringOp },
    Skipped { reason: SkipReason },
}

/// Why an op was skipped by `SubmitPolicy::SkipIfClaimed`.
#[derive(Clone, Debug)]
pub enum SkipReason {
    ClaimedByHigherPriority {
        existing_priority: Priority,
        existing_reason: &'static str,
    },
}

#[derive(Clone, Debug)]
struct CommittedRename {
    new_name: Atom,
    priority: Priority,
    reason: &'static str,
}

#[derive(Clone, Debug)]
struct CommittedMove {
    to: ModuleId,
    reason: &'static str,
}

pub struct LoweringPlan {
    /// Pristine name pool per scope, plus every committed
    /// rename's `new_name`. `is_name_taken` walks ancestors.
    occupied: HashMap<Scope, HashSet<Atom>>,
    function_parents: Vec<Scope>,
    residual: ModuleId,
    modules: Vec<ModuleId>,
    renames: HashMap<(Scope, Id), CommittedRename>,
    moves: HashMap<Id, CommittedMove>,
}

#[derive(Debug, Default)]
pub struct CheckedPlan {
    pub rename_index: HashMap<(Scope, Id), Atom>,
    pub move_index: HashMap<Id, ModuleId>,
}

impl LoweringPlan {
    /// `occupied_by_scope` is the pristine name pool: every
    /// identifier already bound at each scope before any
    /// contributor runs.
    pub fn new(
        residual: ModuleId,
        modules: Vec<ModuleId>,
        occupied_by_scope: HashMap<Scope, HashSet<Atom>>,
    ) -> Self {
        Self {
            occupied: occupied_by_scope,
            function_parents: Vec::new(),
            residual,
            modules,
            renames: HashMap::new(),
            moves: HashMap::new(),
        }
    }

    pub fn mint_function_scope(&mut self, parent: Scope) -> FunctionScopeId {
        let id = FunctionScopeId(self.function_parents.len());
        self.function_parents.push(parent);
        id
    }

    /// Extend a scope's occupied pool after construction (for
    /// late-discovered bindings — e.g. names declared inside
    /// function bodies that the chunk-level seed missed).
    pub fn extend_occupied(&mut self, scope: Scope, names: impl IntoIterator<Item = Atom>) {
        let entry = self.occupied.entry(scope).or_default();
        for name in names {
            entry.insert(name);
        }
    }

    pub fn modules(&self) -> &[ModuleId] {
        &self.modules
    }

    pub fn residual(&self) -> ModuleId {
        self.residual
    }

    /// Highest priority that has claimed `(scope, original)`, or
    /// `None`. Exact-slot only — ancestor scopes would refer to
    /// a different binding.
    pub fn is_claimed(&self, scope: Scope, original: &Id) -> Option<Priority> {
        self.renames
            .get(&(scope, original.clone()))
            .map(|r| r.priority)
    }

    /// `true` iff `name` is held at `scope` or any enclosing scope.
    pub fn is_name_taken(&self, scope: Scope, name: &Atom) -> bool {
        let mut cur = Some(scope);
        while let Some(s) = cur {
            if self.occupied.get(&s).is_some_and(|set| set.contains(name)) {
                return true;
            }
            cur = self.parent_of(s);
        }
        false
    }

    fn parent_of(&self, scope: Scope) -> Option<Scope> {
        match scope {
            Scope::Chunk => None,
            Scope::Module(_) => Some(Scope::Chunk),
            Scope::Function(f) => self.function_parents.get(f.0).copied(),
        }
    }

    /// `Ok(Accepted { final_op })` with the committed op
    /// (`MintOrSuffix` may carry a suffixed name);
    /// `Ok(Skipped { … })` if `SubmitPolicy::SkipIfClaimed`
    /// dropped it; `Err(_)` for spec-author-facing diagnostics
    /// (same-priority disagreement, `Required` name taken,
    /// invalid identifier, two `MoveBinding`s on the same id).
    pub fn submit(&mut self, op: LoweringOp, on_conflict: SubmitPolicy) -> Result<SubmitOutcome> {
        match op {
            LoweringOp::Rename {
                scope,
                original,
                name,
                reason,
                priority,
            } => self.submit_rename(scope, original, name, reason, priority, on_conflict),
            LoweringOp::MoveBinding { id, to, reason } => {
                self.submit_move(id, to, reason, on_conflict)
            }
        }
    }

    fn submit_rename(
        &mut self,
        scope: Scope,
        original: Id,
        name: NamePolicy,
        reason: &'static str,
        priority: Priority,
        on_conflict: SubmitPolicy,
    ) -> Result<SubmitOutcome> {
        if let Some(existing) = self.renames.get(&(scope, original.clone())) {
            // Same-priority disagreement always errors, regardless
            // of policy. Identical resubmission (same priority, same
            // committed `new_name`) collapses silently — letting two
            // contributors derive the same conclusion isn't a bug.
            if existing.priority == priority {
                if name_matches_committed(&name, &existing.new_name) {
                    return Ok(SubmitOutcome::Accepted {
                        final_op: LoweringOp::Rename {
                            scope,
                            original,
                            name: NamePolicy::Required(existing.new_name.clone()),
                            reason,
                            priority,
                        },
                    });
                }
                let requested = requested_name(&name);
                let existing_reason = existing.reason;
                let existing_new = &existing.new_name;
                let orig = &original.0;
                return Err(anyhow!(
                    "rename conflict at same priority {priority:?}: \
                     binding {orig:?} in {scope:?} — {reason} wants {requested} \
                     but {existing_reason} already committed {existing_new}"
                ));
            }
            // Different-priority conflict: policy decides.
            let orig = &original.0;
            let existing_reason = existing.reason;
            let existing_priority = existing.priority;
            let existing_new = &existing.new_name;
            return match on_conflict {
                SubmitPolicy::Fail => Err(anyhow!(
                    "rename conflict: binding {orig:?} in {scope:?} already claimed by \
                     {existing_reason} at priority {existing_priority:?} (committed \
                     {existing_new}); {reason} at priority {priority:?} submitted with \
                     SubmitPolicy::Fail"
                )),
                SubmitPolicy::SkipIfClaimed => {
                    if existing_priority > priority {
                        Ok(SubmitOutcome::Skipped {
                            reason: SkipReason::ClaimedByHigherPriority {
                                existing_priority,
                                existing_reason,
                            },
                        })
                    } else {
                        // Stratified submission orders contributors
                        // highest-priority-first; a higher-priority
                        // op arriving after a lower-priority claim
                        // is an orchestrator bug, not a silent
                        // override.
                        Err(anyhow!(
                            "rename submission order violation: incoming op {reason} \
                             at priority {priority:?} arrived after lower-priority op \
                             {existing_reason} at priority {existing_priority:?} for \
                             binding {orig:?} in {scope:?}. Phases must submit in \
                             highest-priority-first order."
                        ))
                    }
                }
            };
        }
        // No existing claim — pick the final name per NamePolicy.
        let final_name = self.resolve_name(scope, &name, reason)?;
        self.renames.insert(
            (scope, original.clone()),
            CommittedRename {
                new_name: final_name.clone(),
                priority,
                reason,
            },
        );
        self.occupied
            .entry(scope)
            .or_default()
            .insert(final_name.clone());
        Ok(SubmitOutcome::Accepted {
            final_op: LoweringOp::Rename {
                scope,
                original,
                name: NamePolicy::Required(final_name),
                reason,
                priority,
            },
        })
    }

    fn submit_move(
        &mut self,
        id: Id,
        to: ModuleId,
        reason: &'static str,
        on_conflict: SubmitPolicy,
    ) -> Result<SubmitOutcome> {
        if let Some(existing) = self.moves.get(&id) {
            if existing.to == to {
                return Ok(SubmitOutcome::Accepted {
                    final_op: LoweringOp::MoveBinding { id, to, reason },
                });
            }
            let orig = &id.0;
            let existing_to = existing.to;
            let existing_reason = existing.reason;
            return match on_conflict {
                SubmitPolicy::Fail => Err(anyhow!(
                    "move conflict: binding {orig:?} already routed to module \
                     {existing_to:?} by {existing_reason}; {reason} submitted a move \
                     to {to:?} with SubmitPolicy::Fail"
                )),
                SubmitPolicy::SkipIfClaimed => Ok(SubmitOutcome::Skipped {
                    reason: SkipReason::ClaimedByHigherPriority {
                        existing_priority: Priority::Explicit,
                        existing_reason,
                    },
                }),
            };
        }
        self.moves.insert(id.clone(), CommittedMove { to, reason });
        Ok(SubmitOutcome::Accepted {
            final_op: LoweringOp::MoveBinding { id, to, reason },
        })
    }

    fn resolve_name(&self, scope: Scope, name: &NamePolicy, reason: &'static str) -> Result<Atom> {
        match name {
            NamePolicy::Required(atom) => {
                if !is_valid_js_identifier(atom.as_str()) {
                    return Err(anyhow!(
                        "rename target {atom} (from {reason}) is not a valid JS identifier"
                    ));
                }
                if self.is_name_taken(scope, atom) {
                    return Err(anyhow!(
                        "rename target {atom} (from {reason}) is already taken in {scope:?}"
                    ));
                }
                Ok(atom.clone())
            }
            NamePolicy::MintOrSuffix(base) => {
                if !is_valid_js_identifier(base.as_str()) {
                    return Err(anyhow!(
                        "mint base {base} (from {reason}) is not a valid JS identifier"
                    ));
                }
                if !self.is_name_taken(scope, base) {
                    return Ok(base.clone());
                }
                let mut n: u32 = 1;
                loop {
                    let candidate: Atom = format!("{base}_{n}").into();
                    if !self.is_name_taken(scope, &candidate) {
                        return Ok(candidate);
                    }
                    n = n.checked_add(1).ok_or_else(|| {
                        anyhow!(
                            "MintOrSuffix exhausted u32 suffix space for base {base} \
                             (from {reason}) in {scope:?}"
                        )
                    })?;
                }
            }
        }
    }

    /// Snapshot the plan into a [`CheckedPlan`].
    ///
    /// Runs the cross-op coherence check that submit-time can't
    /// catch (it depends on *combinations* of ops): if a binding
    /// has a `MoveBinding { to: M }`, every `Rename` op on that
    /// binding must apply where the binding lives — `Scope::Chunk`
    /// is always OK; `Scope::Module(N)` for `N != M` is not;
    /// `Scope::Function(_)` is conservatively accepted (function
    /// scopes don't carry an enclosing-module link).
    ///
    /// All violations collected; the returned error embeds them
    /// so spec authors see the full set in one round-trip.
    pub fn seal(self) -> Result<CheckedPlan> {
        let mut errors: Vec<String> = Vec::new();
        for ((scope, original), rename) in &self.renames {
            if let Some(committed_move) = self.moves.get(original) {
                let move_to = committed_move.to;
                let coherent = match scope {
                    Scope::Chunk => true,
                    Scope::Module(m) => *m == move_to,
                    // Function scopes don't carry an enclosing
                    // module link; conservatively accept.
                    Scope::Function(_) => true,
                };
                if !coherent {
                    let rename_reason = rename.reason;
                    let move_reason = committed_move.reason;
                    let new_name = &rename.new_name;
                    let orig_atom = &original.0;
                    errors.push(format!(
                        "rename/move incoherence: binding {orig_atom:?} moves to module \
                         {move_to:?} (by {move_reason}) but {rename_reason} renamed it \
                         to {new_name} at {scope:?}"
                    ));
                }
            }
        }
        if !errors.is_empty() {
            return Err(anyhow!(
                "lowering plan seal rejected {} violation(s):\n  - {}",
                errors.len(),
                errors.join("\n  - ")
            ));
        }
        let rename_index = self
            .renames
            .into_iter()
            .map(|((scope, id), r)| ((scope, id), r.new_name))
            .collect();
        let move_index = self.moves.into_iter().map(|(id, m)| (id, m.to)).collect();
        Ok(CheckedPlan {
            rename_index,
            move_index,
        })
    }
}

fn name_matches_committed(policy: &NamePolicy, committed: &Atom) -> bool {
    match policy {
        NamePolicy::Required(atom) => atom == committed,
        // Two contributors that both said "give me x or something
        // _N" can't be sure to land on the same final name, so we
        // can't treat them as idempotent. Conservative: not a match.
        NamePolicy::MintOrSuffix(_) => false,
    }
}

fn requested_name(policy: &NamePolicy) -> &Atom {
    match policy {
        NamePolicy::Required(atom) | NamePolicy::MintOrSuffix(atom) => atom,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use swc_common::SyntaxContext;

    fn id(name: &str) -> Id {
        (Atom::from(name), SyntaxContext::empty())
    }

    fn module(n: usize) -> ModuleId {
        ModuleId::logical(n)
    }

    fn plan() -> LoweringPlan {
        LoweringPlan::new(module(0), vec![module(0), module(1)], HashMap::new())
    }

    fn plan_with_chunk_occupied(names: &[&str]) -> LoweringPlan {
        let mut occupied = HashMap::new();
        occupied.insert(
            Scope::Chunk,
            names.iter().map(|s| Atom::from(*s)).collect::<HashSet<_>>(),
        );
        LoweringPlan::new(module(0), vec![module(0)], occupied)
    }

    fn assert_accepted_with_name(outcome: SubmitOutcome, expected: &str) {
        match outcome {
            SubmitOutcome::Accepted {
                final_op:
                    LoweringOp::Rename {
                        name: NamePolicy::Required(atom),
                        ..
                    },
            } => {
                assert_eq!(atom.as_str(), expected);
            }
            other => panic!("expected Accepted with Required({expected}), got {other:?}"),
        }
    }

    #[test]
    fn required_name_accepted_when_free() {
        let mut p = plan();
        let out = p
            .submit(
                LoweringOp::Rename {
                    scope: Scope::Chunk,
                    original: id("orig"),
                    name: NamePolicy::Required(Atom::from("newName")),
                    reason: "test",
                    priority: Priority::Explicit,
                },
                SubmitPolicy::Fail,
            )
            .unwrap();
        assert_accepted_with_name(out, "newName");
        assert!(p.is_name_taken(Scope::Chunk, &Atom::from("newName")));
        assert_eq!(
            p.is_claimed(Scope::Chunk, &id("orig")),
            Some(Priority::Explicit)
        );
    }

    #[test]
    fn required_errors_when_name_taken_in_pristine_pool() {
        let mut p = plan_with_chunk_occupied(&["existing"]);
        let err = p
            .submit(
                LoweringOp::Rename {
                    scope: Scope::Chunk,
                    original: id("orig"),
                    name: NamePolicy::Required(Atom::from("existing")),
                    reason: "test",
                    priority: Priority::Explicit,
                },
                SubmitPolicy::Fail,
            )
            .unwrap_err();
        assert!(err.to_string().contains("already taken"));
    }

    #[test]
    fn mint_or_suffix_picks_base_when_free() {
        let mut p = plan();
        let out = p
            .submit(
                LoweringOp::Rename {
                    scope: Scope::Chunk,
                    original: id("orig"),
                    name: NamePolicy::MintOrSuffix(Atom::from("base")),
                    reason: "test",
                    priority: Priority::Collision,
                },
                SubmitPolicy::Fail,
            )
            .unwrap();
        assert_accepted_with_name(out, "base");
    }

    #[test]
    fn mint_or_suffix_iterates_through_suffixes() {
        let mut p = plan_with_chunk_occupied(&["foo", "foo_1", "foo_2"]);
        let out = p
            .submit(
                LoweringOp::Rename {
                    scope: Scope::Chunk,
                    original: id("orig"),
                    name: NamePolicy::MintOrSuffix(Atom::from("foo")),
                    reason: "test",
                    priority: Priority::Collision,
                },
                SubmitPolicy::Fail,
            )
            .unwrap();
        assert_accepted_with_name(out, "foo_3");
    }

    #[test]
    fn skip_if_claimed_defers_to_higher_priority() {
        let mut p = plan();
        p.submit(
            LoweringOp::Rename {
                scope: Scope::Chunk,
                original: id("orig"),
                name: NamePolicy::Required(Atom::from("explicit")),
                reason: "spec",
                priority: Priority::Explicit,
            },
            SubmitPolicy::Fail,
        )
        .unwrap();
        let out = p
            .submit(
                LoweringOp::Rename {
                    scope: Scope::Chunk,
                    original: id("orig"),
                    name: NamePolicy::Required(Atom::from("heuristic")),
                    reason: "naturalizer",
                    priority: Priority::Heuristic,
                },
                SubmitPolicy::SkipIfClaimed,
            )
            .unwrap();
        assert!(matches!(out, SubmitOutcome::Skipped { .. }));
        // Explicit's commit survives.
        assert!(p.is_name_taken(Scope::Chunk, &Atom::from("explicit")));
        assert!(!p.is_name_taken(Scope::Chunk, &Atom::from("heuristic")));
    }

    #[test]
    fn same_priority_disagreement_errors_regardless_of_policy() {
        let mut p = plan();
        p.submit(
            LoweringOp::Rename {
                scope: Scope::Chunk,
                original: id("orig"),
                name: NamePolicy::Required(Atom::from("a")),
                reason: "first",
                priority: Priority::Heuristic,
            },
            SubmitPolicy::SkipIfClaimed,
        )
        .unwrap();
        let err = p
            .submit(
                LoweringOp::Rename {
                    scope: Scope::Chunk,
                    original: id("orig"),
                    name: NamePolicy::Required(Atom::from("b")),
                    reason: "second",
                    priority: Priority::Heuristic,
                },
                SubmitPolicy::SkipIfClaimed,
            )
            .unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("same priority"));
        assert!(msg.contains("first"));
        assert!(msg.contains("second"));
    }

    #[test]
    fn same_priority_identical_resubmission_is_idempotent() {
        let mut p = plan();
        for reason in ["first", "second"] {
            p.submit(
                LoweringOp::Rename {
                    scope: Scope::Chunk,
                    original: id("orig"),
                    name: NamePolicy::Required(Atom::from("agreed")),
                    reason,
                    priority: Priority::Heuristic,
                },
                SubmitPolicy::SkipIfClaimed,
            )
            .unwrap();
        }
        let checked = p.seal().unwrap();
        assert_eq!(
            checked.rename_index.get(&(Scope::Chunk, id("orig"))),
            Some(&Atom::from("agreed"))
        );
    }

    #[test]
    fn fail_policy_errors_on_any_existing_claim() {
        let mut p = plan();
        p.submit(
            LoweringOp::Rename {
                scope: Scope::Chunk,
                original: id("orig"),
                name: NamePolicy::Required(Atom::from("h")),
                reason: "heur",
                priority: Priority::Heuristic,
            },
            SubmitPolicy::Fail,
        )
        .unwrap();
        let err = p
            .submit(
                LoweringOp::Rename {
                    scope: Scope::Chunk,
                    original: id("orig"),
                    name: NamePolicy::Required(Atom::from("e")),
                    reason: "explicit",
                    priority: Priority::Explicit,
                },
                SubmitPolicy::Fail,
            )
            .unwrap_err();
        assert!(err.to_string().contains("already claimed"));
    }

    #[test]
    fn is_name_taken_walks_lexical_chain() {
        let mut p = plan_with_chunk_occupied(&["chunkName"]);
        // Module scope doesn't have "chunkName" directly, but Chunk
        // does — the walk should see it.
        assert!(p.is_name_taken(Scope::Module(module(0)), &Atom::from("chunkName")));
        // Module-only name doesn't leak the other direction.
        p.occupied
            .entry(Scope::Module(module(0)))
            .or_default()
            .insert(Atom::from("modName"));
        assert!(!p.is_name_taken(Scope::Chunk, &Atom::from("modName")));
        assert!(p.is_name_taken(Scope::Module(module(0)), &Atom::from("modName")));
    }

    #[test]
    fn move_binding_accepted_and_indexed() {
        let mut p = plan();
        let out = p
            .submit(
                LoweringOp::MoveBinding {
                    id: id("foo"),
                    to: module(1),
                    reason: "spec",
                },
                SubmitPolicy::Fail,
            )
            .unwrap();
        assert!(matches!(out, SubmitOutcome::Accepted { .. }));
        let checked = p.seal().unwrap();
        assert_eq!(checked.move_index.get(&id("foo")), Some(&module(1)));
    }

    #[test]
    fn move_binding_conflict_errors_under_fail() {
        let mut p = plan();
        p.submit(
            LoweringOp::MoveBinding {
                id: id("foo"),
                to: module(1),
                reason: "spec",
            },
            SubmitPolicy::Fail,
        )
        .unwrap();
        let err = p
            .submit(
                LoweringOp::MoveBinding {
                    id: id("foo"),
                    to: module(2),
                    reason: "materializer",
                },
                SubmitPolicy::Fail,
            )
            .unwrap_err();
        assert!(err.to_string().contains("already routed"));
    }

    #[test]
    fn move_binding_same_destination_is_idempotent() {
        let mut p = plan();
        for reason in ["spec", "materializer"] {
            p.submit(
                LoweringOp::MoveBinding {
                    id: id("foo"),
                    to: module(1),
                    reason,
                },
                SubmitPolicy::Fail,
            )
            .unwrap();
        }
    }

    #[test]
    fn function_scope_walks_through_parent_chain() {
        let mut p = plan_with_chunk_occupied(&["outer"]);
        let mod0 = Scope::Module(module(0));
        p.occupied
            .entry(mod0)
            .or_default()
            .insert(Atom::from("modlocal"));
        let f = p.mint_function_scope(mod0);
        assert!(p.is_name_taken(Scope::Function(f), &Atom::from("outer")));
        assert!(p.is_name_taken(Scope::Function(f), &Atom::from("modlocal")));
        assert!(!p.is_name_taken(Scope::Function(f), &Atom::from("nope")));
    }

    #[test]
    fn seal_accepts_chunk_scope_rename_with_move() {
        let mut p = plan();
        p.submit(
            LoweringOp::Rename {
                scope: Scope::Chunk,
                original: id("foo"),
                name: NamePolicy::Required(Atom::from("foo_renamed")),
                reason: "chunk_renames",
                priority: Priority::Explicit,
            },
            SubmitPolicy::Fail,
        )
        .unwrap();
        p.submit(
            LoweringOp::MoveBinding {
                id: id("foo"),
                to: module(1),
                reason: "spec",
            },
            SubmitPolicy::Fail,
        )
        .unwrap();
        p.seal().unwrap();
    }

    #[test]
    fn seal_accepts_module_rename_in_destination_module() {
        let mut p = plan();
        p.submit(
            LoweringOp::Rename {
                scope: Scope::Module(module(1)),
                original: id("foo"),
                name: NamePolicy::Required(Atom::from("foo_renamed")),
                reason: "naturalizer",
                priority: Priority::Heuristic,
            },
            SubmitPolicy::Fail,
        )
        .unwrap();
        p.submit(
            LoweringOp::MoveBinding {
                id: id("foo"),
                to: module(1),
                reason: "spec",
            },
            SubmitPolicy::Fail,
        )
        .unwrap();
        p.seal().unwrap();
    }

    #[test]
    fn seal_rejects_module_rename_in_wrong_module() {
        let mut p = plan();
        p.submit(
            LoweringOp::Rename {
                scope: Scope::Module(module(0)),
                original: id("foo"),
                name: NamePolicy::Required(Atom::from("foo_renamed")),
                reason: "stale_naturalizer",
                priority: Priority::Heuristic,
            },
            SubmitPolicy::Fail,
        )
        .unwrap();
        p.submit(
            LoweringOp::MoveBinding {
                id: id("foo"),
                to: module(1),
                reason: "spec",
            },
            SubmitPolicy::Fail,
        )
        .unwrap();
        let err = p.seal().unwrap_err().to_string();
        assert!(err.contains("rename/move incoherence"));
        assert!(err.contains("stale_naturalizer"));
        assert!(err.contains("spec"));
    }

    #[test]
    fn seal_batches_multiple_coherence_errors() {
        let mut p = LoweringPlan::new(
            module(0),
            vec![module(0), module(1), module(2)],
            HashMap::new(),
        );
        for (binding, scope) in [
            ("a", Scope::Module(module(0))),
            ("b", Scope::Module(module(0))),
        ] {
            p.submit(
                LoweringOp::Rename {
                    scope,
                    original: id(binding),
                    name: NamePolicy::Required(Atom::from(format!("{binding}_x").as_str())),
                    reason: "test",
                    priority: Priority::Heuristic,
                },
                SubmitPolicy::Fail,
            )
            .unwrap();
            p.submit(
                LoweringOp::MoveBinding {
                    id: id(binding),
                    to: module(2),
                    reason: "spec",
                },
                SubmitPolicy::Fail,
            )
            .unwrap();
        }
        let err = p.seal().unwrap_err().to_string();
        assert!(err.contains("2 violation(s)"));
        assert!(err.contains("\"a\""));
        assert!(err.contains("\"b\""));
    }
}
