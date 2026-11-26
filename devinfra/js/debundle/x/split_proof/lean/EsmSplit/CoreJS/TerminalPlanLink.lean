import EsmSplit.CoreJS.TerminalSource
import EsmSplit.CoreJS.OrderedRegion

/-! Link the actual import-free singleton source to the actual full-plan split.
The scope premise must be established separately from source and plan facts;
in particular this file does not infer it from a projected executable order. -/

namespace CoreJS.TerminalPlanLink

open LinkedBehavior

def source (name : Name) (exports : List (Name × Name)) (ss : List Stmt) : Program :=
  ⟨name, [⟨name, [], (List.range ss.length).zip ss, exports⟩]⟩

/-- Changing the singleton's name and export metadata does not alter its
allocation regions or the identity-sorted declarations used by the linker. -/
theorem source_statements (name : Name) (exports : List (Name × Name)) (ss : List Stmt) :
    (source name exports ss).regions = (Program.single ss).regions ∧
    (source name exports ss).tops = (Program.single ss).tops := by
  constructor
  · simp [source, Program.regions, Program.single]
  · simp [source, Program.tops, Program.loaded, Program.evalOrder, Program.visit,
      EsmSplit.visitImports, Program.find, Program.single]

/-- The full plan's executable order and per-unit binding agreement are
enough for scope agreement. The caller must prove the candidate scopes exist
and resolve each unit's free bindings from the actual graph and frames. -/
theorem scopes_of_units (name : Name) (exports : List (Name × Name)) (ss : List Stmt)
    (B : EsmSplit.Bundle) (P : EsmSplit.Plan)
    (ordered : ProjectedOrder.Kept ss P)
    (candidateScopes : ∀ frames, FramesOf ((List.range ss.length).zip ss) frames →
      ∃ linked, (splitProgram ss B P).scopes frames (splitProgram ss B P).loaded = .ok linked ∧
        ∀ u ∈ unitsOf linked, Agree u.2.1.free (bundleEnv ss frames) u.2.2) :
    ScopesAgree (source name exports ss) (splitProgram ss B P) := by
  intro frames hframes
  rw [(source_statements name exports ss).2, single_tops] at hframes
  obtain ⟨linked, linkedScopes, agrees⟩ := candidateScopes frames hframes
  let m : Module := ⟨name, [], (List.range ss.length).zip ss, exports⟩
  let env := bundleEnv ss frames
  have sourceLoaded : (source name exports ss).loaded = [m] := by
    simp [source, m, Program.loaded, Program.evalOrder, Program.visit, EsmSplit.visitImports, Program.find]
  have sourceScopes : (source name exports ss).scopes frames (source name exports ss).loaded = .ok [(m, env)] := by
    rw [sourceLoaded]
    simp [m, env, bundleEnv, bundleModule, Module.scope, Program.scopes, Program.imported] <;> rfl
  have sourceKeys : (unitsOf [(m, env)]).map (fun u => (u.1, u.2.1)) = (List.range ss.length).zip ss := by
    rw [unitsOf_keys]
    simp [m]
  have candidateKeys : (unitsOf linked).map (fun u => (u.1, u.2.1)) = (List.range ss.length).zip ss := by
    rw [unitsOf_keys]
    have modules := ProjectedOrder.scopes_modules (splitProgram ss B P) frames _ _ linkedScopes
    rw [show linked.flatMap (·.1.body) = (linked.map Prod.fst).flatMap (·.body) by rw [List.flatMap_map]]
    rw [modules, split_loaded_flat, ← ProjectedOrder.numbered_filter]
    change (ProjectedOrder.executable ss P).filterMap (numbered ss) = _
    rw [ordered, filterMap_numbered_range]
  refine ⟨[(m, env)], linked, sourceScopes, linkedScopes, ?_, ?_⟩
  · refine sameUnits_of (sourceKeys.trans candidateKeys.symm) ?_
    intro u hu u' hu' _ hs
    obtain ⟨p, hp, _, he⟩ := mem_unitsOf hu
    rw [List.mem_singleton] at hp
    subst p
    rw [he, hs]
    exact agrees u' hu'
  · have h := zip_range_sorted ss
    rw [← sourceKeys] at h
    exact (List.pairwise_map (R := fun a b => a.1 ≤ b.1) (f := fun u : LinkedUnit => (u.1, u.2.1))).1 h

/-- Resolve each actual loaded module's imports and show agreement at its
actual executable statements; then the real candidate scopes exist and have
the unit-by-unit agreement needed above. -/
theorem units_of_modules (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan)
    (frames : List (Nat × List (Name × Addr)))
    (hmodules : ∀ m ∈ (splitProgram ss B P).loaded, ∃ cells,
      (splitProgram ss B P).imported frames m.imports = .ok cells ∧
      ∀ p ∈ m.body, Agree p.2.free (bundleEnv ss frames)
        { vars := m.scope frames ++ cells, this := .undef }) :
    ∃ linked, (splitProgram ss B P).scopes frames (splitProgram ss B P).loaded = .ok linked ∧
      ∀ u ∈ unitsOf linked, Agree u.2.1.free (bundleEnv ss frames) u.2.2 := by
  obtain ⟨linked, ok, _, members⟩ := scopes_ok (splitProgram ss B P) frames
    (fun m cells => ∀ p ∈ m.body, Agree p.2.free (bundleEnv ss frames)
      { vars := m.scope frames ++ cells, this := .undef }) _ hmodules
  refine ⟨linked, ok, ?_⟩
  intro u hu
  obtain ⟨p, hp, hb, he⟩ := mem_unitsOf hu
  obtain ⟨cells, agree, scope⟩ := members p hp
  rw [he, scope]
  exact agree (u.1, u.2.1) hb

/-- A successful source link yields the actual candidate link, with identical
post-hoist state and captured ordered schedule. `scopes` is deliberately an
explicit premise until the full metadata-aware graph proof provides it. -/
theorem candidate_link (name : Name) (exports : List (Name × Name)) (ss : List Stmt)
    (B : EsmSplit.Bundle) (P : EsmSplit.Plan)
    (coverage : (ProjectedOrder.executable ss P).Perm (List.range ss.length))
    (scopes : ScopesAgree (source name exports ss) (splitProgram ss B P))
    (w : World) (before : CheckedLink (source name exports ss) w) :
    ∃ after : CheckedLink (splitProgram ss B P) w,
      before.ready = after.ready ∧
      SameUnits (unitsOf before.linked) (unitsOf after.linked) ∧
      captureUnits (unitsOf before.linked) = captureUnits (unitsOf after.linked) ∧
      ∀ fuel, runProgram w fuel (source name exports ss) = runProgram w fuel (splitProgram ss B P) := by
  have facts := ProjectedOrder.holds_statements (B := B) coverage
  have original := source_statements name exports ss
  obtain ⟨after, ready, same, schedule⟩ := OrderedRegion.matching_link
    (original.1.trans facts.1) (original.2.trans facts.2) scopes before
  exact ⟨after, ready, same, schedule, fun fuel => run_eq_of_ordered_links fuel before after ready schedule⟩

/-- Compose the actual-source link with per-module import and binding facts
about the full planned graph. No candidate link or completed scope result is
assumed; those are constructed for the same world and initial state. -/
theorem candidate_link_of_modules (name : Name) (exports : List (Name × Name)) (ss : List Stmt)
    (B : EsmSplit.Bundle) (P : EsmSplit.Plan) (ordered : ProjectedOrder.Kept ss P)
    (hmodules : ∀ frames, FramesOf ((List.range ss.length).zip ss) frames →
      ∀ m ∈ (splitProgram ss B P).loaded, ∃ cells,
        (splitProgram ss B P).imported frames m.imports = .ok cells ∧
        ∀ p ∈ m.body, Agree p.2.free (bundleEnv ss frames)
          { vars := m.scope frames ++ cells, this := .undef })
    (w : World) (before : CheckedLink (source name exports ss) w) :
    ∃ after : CheckedLink (splitProgram ss B P) w,
      before.ready = after.ready ∧
      SameUnits (unitsOf before.linked) (unitsOf after.linked) ∧
      captureUnits (unitsOf before.linked) = captureUnits (unitsOf after.linked) ∧
      ∀ fuel, runProgram w fuel (source name exports ss) = runProgram w fuel (splitProgram ss B P) := by
  have coverage : (ProjectedOrder.executable ss P).Perm (List.range ss.length) := by
    rw [ordered]
  have scopes := scopes_of_units name exports ss B P ordered fun frames hframes =>
    units_of_modules ss B P frames (hmodules frames hframes)
  exact candidate_link name exports ss B P coverage scopes w before

#print axioms source_statements
#print axioms scopes_of_units
#print axioms units_of_modules
#print axioms candidate_link
#print axioms candidate_link_of_modules

end CoreJS.TerminalPlanLink
