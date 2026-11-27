import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.SingleModule
import EsmSplit.CoreJS.Split
import EsmSplit.Split

/-! A build-time link invariant for order-preserving planner output. -/

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

/-- Given the real source link, scope agreement constructs the real split link
with exactly the same state after hoisting and the same captured schedule. -/
theorem matching_link {source candidate : Program} {w : World}
    (regions : source.regions = candidate.regions) (tops : source.tops = candidate.tops)
    (scopes : ScopesAgree source candidate) (before : CheckedLink source w) :
    ∃ after : CheckedLink candidate w, before.ready = after.ready ∧
      SameUnits (unitsOf before.linked) (unitsOf after.linked) ∧
      captureUnits (unitsOf before.linked) = captureUnits (unitsOf after.linked) := by
  have linked : ((do
      checkIdentities source.tops
      let frames ← declareTops source.tops
      let linked ← lift (source.scopes frames source.loaded)
      initFunctions ((unitsOf linked).mergeSort fun a b => decide (a.1 ≤ b.1))
      pure linked) : M w _) (initialState w source.regions) = .ok before.linked before.ready := by
    simpa [link, linkUsingScopes] using before.equation
  obtain ⟨_, afterCheck, check, linked⟩ := bind_ok linked
  obtain ⟨frames, afterDeclare, declared, linked⟩ := bind_ok linked
  obtain ⟨L, L', sourceScope, candidateScope, same, sorted⟩ :=
    scopes frames (declareTops_names _ declared)
  rw [sourceScope] at linked
  change (initFunctions ((unitsOf L).mergeSort fun a b => decide (a.1 ≤ b.1)) >>= fun _ => pure L)
    afterDeclare = .ok before.linked before.ready at linked
  obtain ⟨ignored, final, initialized, returned⟩ := bind_ok linked
  cases ignored
  change Res.ok L final = .ok before.linked before.ready at returned
  have sourceLinked : L = before.linked := by injection returned
  have sameState : final = before.ready := by injection returned
  have sorted' : (unitsOf L').Pairwise fun a b => a.1 ≤ b.1 := by
    have h := (List.pairwise_map (f := fun u : LinkedUnit => u.1) (R := fun a b => a ≤ b)).2 sorted
    rw [sameUnit_ids same] at h
    exact List.pairwise_map.1 h
  rw [sorted_units sorted, initFunctions_same same, ← sorted_units sorted'] at initialized
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
  refine ⟨after, rfl, ?_, ?_⟩
  · simpa [after, ← sourceLinked] using same
  · apply sameUnits_capture
    simpa [after, ← sourceLinked] using same

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
