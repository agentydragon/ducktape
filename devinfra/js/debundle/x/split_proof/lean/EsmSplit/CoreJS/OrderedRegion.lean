import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.SingleModule
import EsmSplit.CoreJS.Split
import EsmSplit.Split

/-! Build-time link invariants for planner output. -/

namespace CoreJS.OrderedRegion

open LinkedBehavior

/-- Matching unit identities, statements and bindings imply identical captured
schedules. This includes the receiver and home object as well as free cells. -/
theorem sameUnits_capture {left right : List LinkedUnit} (same : SameUnits left right) :
    captureUnits left = captureUnits right := by
  induction same with
  | nil => rfl
  | @cons u v us vs hu _ ih =>
    obtain ⟨id, stmt, env⟩ := u
    obtain ⟨id', stmt', env'⟩ := v
    obtain ⟨ids, stmts, bindings⟩ := hu
    dsimp only [LinkedUnit.id, LinkedUnit.stmt, LinkedUnit.env] at ids stmts bindings
    subst id'
    subst stmt'
    simp only [captureUnits, List.map_cons] at ih ⊢
    rw [capture_agree bindings, ih]

/-- Valid source footprints and the authored plan imply equal original unit
identities and equal name resolution at each ordered use. -/
theorem planned_scopes (ss : List Stmt) (bundle : EsmSplit.Bundle) (spec : EsmSplit.Spec)
    (valid : EsmSplit.Valid bundle spec (EsmSplit.mkPlan bundle spec))
    (footprints : FootprintsOfSyntax ss bundle)
    (ordered : OrderKept ss (EsmSplit.mkPlan bundle spec)) :
    ScopesAgree (Program.single ss) (splitProgram ss bundle (EsmSplit.mkPlan bundle spec)) :=
  split_scopes_agree valid footprints ordered

/-- For any frames with the original bindings, the two actual graphs resolve
their scopes and agree in the identity order used to initialize hoists. -/
def HoistScopesAgree (source candidate : Program) : Prop :=
  ∀ frames, FramesOf source.tops frames → ∃ before after,
    source.scopes frames source.loaded = .ok before ∧
    candidate.scopes frames candidate.loaded = .ok after ∧
    SameUnits ((unitsOf before).mergeSort fun a b => decide (a.id ≤ b.id))
      ((unitsOf after).mergeSort fun a b => decide (a.id ≤ b.id))

/-- Transport a successful source link through declaration and hoist
initialization. The two scope equations use the same allocation frames.
Modules may have different eager evaluation orders. -/
theorem matching_hoist_link_with_frames {source candidate : Program} {w : World}
    (regions : source.regions = candidate.regions) (tops : source.tops = candidate.tops)
    (scopes : HoistScopesAgree source candidate) (before : CheckedLink source w) :
    ∃ (start : State w) (frames : List (Nat × List (Name × Addr))) (allocated : State w)
        (after : CheckedLink candidate w),
      checkIdentities source.tops (initialState w source.regions) = .ok () start ∧
      declareTops source.tops start = .ok frames allocated ∧
      source.scopes frames source.loaded = .ok before.linked ∧
      candidate.scopes frames candidate.loaded = .ok after.linked ∧
      before.ready = after.ready := by
  have linked : ((do
      checkIdentities source.tops
      let frames ← declareTops source.tops
      let linked ← lift (source.scopes frames source.loaded)
      initFunctions ((unitsOf linked).mergeSort fun a b => decide (a.1 ≤ b.1))
      pure linked) : M w _) (initialState w source.regions) = .ok before.linked before.ready := by
    simpa [link, linkUsingScopes] using before.equation
  obtain ⟨checked, afterCheck, check, linked⟩ := bind_ok linked
  cases checked
  obtain ⟨frames, afterDeclare, declared, linked⟩ := bind_ok linked
  obtain ⟨L, L', sourceScope, candidateScope, same⟩ :=
    scopes frames (declareTops_names _ declared)
  rw [sourceScope] at linked
  change (initFunctions ((unitsOf L).mergeSort fun a b => decide (a.1 ≤ b.1)) >>= fun _ => pure L)
    afterDeclare = .ok before.linked before.ready at linked
  obtain ⟨ignored, final, initialized, returned⟩ := bind_ok linked
  cases ignored
  change Res.ok L final = .ok before.linked before.ready at returned
  have sourceLinked : L = before.linked := by injection returned
  have sameState : final = before.ready := by injection returned
  rw [initFunctions_same same] at initialized
  have afterLink : link candidate (initialState w candidate.regions) = .ok L' before.ready := by
    rw [← regions]
    unfold link linkUsingScopes
    rw [← tops]
    simp only [M.bind_apply, check, declared, candidateScope, lift, pure]
    change ((initFunctions ((unitsOf L').mergeSort fun a b => decide (a.1 ≤ b.1)) >>= fun _ => pure L') : M w _)
      afterDeclare = .ok L' before.ready
    rw [M.bind_apply, initialized, sameState]
    rfl
  let after : CheckedLink candidate w := ⟨L', before.ready, afterLink⟩
  exact ⟨afterCheck, frames, afterDeclare, after, check, declared, sourceLinked ▸ sourceScope, candidateScope, rfl⟩

#print axioms matching_hoist_link_with_frames

/-- The source allocation frames are shared by the two ordered links. The
caller can use them to prove that public names resolve to the same cells. -/
theorem matching_link_with_frames {source candidate : Program} {w : World}
    (regions : source.regions = candidate.regions) (tops : source.tops = candidate.tops)
    (scopes : ScopesAgree source candidate) (before : CheckedLink source w) :
    ∃ (start : State w) (frames : List (Nat × List (Name × Addr))) (allocated : State w)
        (after : CheckedLink candidate w),
      declareTops source.tops start = .ok frames allocated ∧
      source.scopes frames source.loaded = .ok before.linked ∧
      candidate.scopes frames candidate.loaded = .ok after.linked ∧
      before.ready = after.ready ∧
      SameUnits (unitsOf before.linked) (unitsOf after.linked) ∧
      captureUnits (unitsOf before.linked) = captureUnits (unitsOf after.linked) := by
  have hoists : HoistScopesAgree source candidate := by
    intro frames bindings
    obtain ⟨L, L', sourceScope, candidateScope, same, sorted⟩ := scopes frames bindings
    have sorted' : (unitsOf L').Pairwise fun a b => a.id ≤ b.id := by
      have ids := (List.pairwise_map (f := fun u : LinkedUnit => u.id) (R := fun a b => a ≤ b)).2 sorted
      rw [sameUnit_ids same] at ids
      exact List.pairwise_map.1 ids
    exact ⟨L, L', sourceScope, candidateScope, by rwa [sorted_units sorted, sorted_units sorted']⟩
  obtain ⟨start, frames, allocated, after, _, declared, sourceScope, candidateScope, ready⟩ :=
    matching_hoist_link_with_frames regions tops hoists before
  obtain ⟨L, L', originalScope, splitScope, same, _⟩ := scopes frames (declareTops_names _ declared)
  have originalLinked := Except.ok.inj (originalScope.symm.trans sourceScope)
  have splitLinked := Except.ok.inj (splitScope.symm.trans candidateScope)
  rw [originalLinked, splitLinked] at same
  exact ⟨start, frames, allocated, after, declared, sourceScope, candidateScope, ready, same, sameUnits_capture same⟩

/-- Given the real source link, scope agreement constructs the real split link
with exactly the same state after hoisting and the same captured schedule. -/
theorem matching_link {source candidate : Program} {w : World}
    (regions : source.regions = candidate.regions) (tops : source.tops = candidate.tops)
    (scopes : ScopesAgree source candidate) (before : CheckedLink source w) :
    ∃ after : CheckedLink candidate w, before.ready = after.ready ∧
      SameUnits (unitsOf before.linked) (unitsOf after.linked) ∧
      captureUnits (unitsOf before.linked) = captureUnits (unitsOf after.linked) := by
  obtain ⟨_, _, _, after, _, _, _, ready, same, captured⟩ :=
    matching_link_with_frames regions tops scopes before
  exact ⟨after, ready, same, captured⟩

#print axioms matching_link_with_frames

/-- The authored source/spec checks imply the actual links agree, and hence
every bounded result agrees, including throws, unknowns and fuel exhaustion. -/
theorem planned_link (ss : List Stmt) (bundle : EsmSplit.Bundle) (spec : EsmSplit.Spec)
    (valid : EsmSplit.Valid bundle spec (EsmSplit.mkPlan bundle spec))
    (footprints : FootprintsOfSyntax ss bundle)
    (ordered : OrderKept ss (EsmSplit.mkPlan bundle spec)) (w : World)
    (before : CheckedLink (Program.single ss) w) :
    ∃ after : CheckedLink (splitProgram ss bundle (EsmSplit.mkPlan bundle spec)) w,
      before.ready = after.ready ∧
      SameUnits (unitsOf before.linked) (unitsOf after.linked) ∧
      captureUnits (unitsOf before.linked) = captureUnits (unitsOf after.linked) ∧
      ∀ fuel, runProgram w fuel (Program.single ss) =
        runProgram w fuel (splitProgram ss bundle (EsmSplit.mkPlan bundle spec)) := by
  obtain ⟨after, ready, same, schedule⟩ := matching_link
    (split_holds_statements (orderKept_perm ordered)).1
    (split_holds_statements (orderKept_perm ordered)).2
    (planned_scopes ss bundle spec valid footprints ordered) before
  exact ⟨after, ready, same, schedule, fun fuel => run_eq_of_ordered_links fuel before after ready schedule⟩

/-- Full bounded modeled outcomes, without running or comparing a candidate.
The only hypotheses are source footprints and the actual authored plan's
static validity and execution order. -/
theorem planned_runs (ss : List Stmt) (bundle : EsmSplit.Bundle) (spec : EsmSplit.Spec)
    (valid : EsmSplit.Valid bundle spec (EsmSplit.mkPlan bundle spec))
    (footprints : FootprintsOfSyntax ss bundle)
    (ordered : OrderKept ss (EsmSplit.mkPlan bundle spec)) (w : World) (fuel : Nat) :
    runProgram w fuel (Program.single ss) =
      runProgram w fuel (splitProgram ss bundle (EsmSplit.mkPlan bundle spec)) :=
  run_same (split_holds_statements (orderKept_perm ordered)).1
    (split_holds_statements (orderKept_perm ordered)).2
    (planned_scopes ss bundle spec valid footprints ordered) fuel

/-- Source export metadata and the original entry name do not affect the
modeled execution result. The separate entry-export obligation must still be
established for the split. -/
theorem planned_runs_with_exports (ss : List Stmt) (bundle : EsmSplit.Bundle) (spec : EsmSplit.Spec)
    (valid : EsmSplit.Valid bundle spec (EsmSplit.mkPlan bundle spec))
    (footprints : FootprintsOfSyntax ss bundle)
    (ordered : OrderKept ss (EsmSplit.mkPlan bundle spec)) (name : String)
    (exports : List (String × String)) (w : World) (fuel : Nat) :
    runProgram w fuel ⟨name, [⟨name, [], (List.range ss.length).zip ss, exports⟩]⟩ =
      runProgram w fuel (splitProgram ss bundle (EsmSplit.mkPlan bundle spec)) := by
  rw [runProgram_singleModuleMetadata _ (zip_range_sorted ss) name exports w fuel]
  exact planned_runs ss bundle spec valid footprints ordered w fuel


end CoreJS.OrderedRegion
