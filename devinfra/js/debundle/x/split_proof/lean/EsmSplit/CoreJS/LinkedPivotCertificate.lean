import EsmSplit.CoreJS.EvaluatorFrame
import EsmSplit.CoreJS.LinkedPivotCheck
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.StateMatch

namespace CoreJS.LinkedBehavior

/-- The finite checks suffice for the same successful final states. The
unmoved code uses the same evaluator and world protocol in both layouts. -/
theorem normalEquivalent_of_checkedPivot {source candidate : Program}
    (checks : ∀ w, CheckedPivot source candidate w) : NormalEquivalent source candidate := by
  refine normalEquivalent_of_pivot (fun w => (checks w).layout) (fun _ => False)
    (fun w => Unprotected ((checks w).layout.pivot.1 + 1) (fun _ => False))
    (fun w => (checks w).access) (fun w => localPivotOk_sound (checks w).localFlag) ?_
  intro w n u hu
  obtain ⟨away, scope⟩ := (checks w).scope u hu
  refine ⟨fun eq => away (congrArg (· + 1) eq), [], by simp, ?_⟩
  rw [← capture_faithful n u.2.2 u.2.1 u.2.1.free (fun _ h => h)]
  have frames := evaluatorFrames w (r := (checks w).layout.pivot.1 + 1) (by omega)
    (R := fun _ => False) (fun _ _ h => h) n
  have step := frames.stmt [] (u.2.2.capture u.2.1.free) u.2.1 scope
  refine ⟨step.replace, step.preserves, ?_⟩
  intro s access away
  have result := step.accessible s access away
  cases h : execStmt n (u.2.2.capture u.2.1.free) u.2.1 s <;> rw [h] at result
  case ok v t => exact ⟨trivial, result.2⟩
  all_goals exact result

private def CheckedLink.forWorld {p : Program} (checked : CheckedLink p World.probe) (w : World) :
    CheckedLink p w :=
  ⟨checked.linked, linkState w checked.ready, link_from_probe checked w⟩

private def checkLink (p : Program) : Except String (CheckedLink p World.probe) :=
  match h : link p (initialState World.probe p.regions) with
  | .ok linked ready => .ok ⟨linked, ready, h⟩
  | .unknown reason _ => .error reason
  | .throw _ _ => .error "the module graph threw during linking"
  | .fuel => .error "linking exhausted fuel"

private theorem Access.linkState {w cells A} {s : State World.probe} (access : Access cells A s) :
    Access cells A (linkState w s) := by
  refine ⟨access.handed, access.supplied, ?_⟩
  intro src dst hsrc edge
  cases edge with
  | obj ho hm => exact access.closed _ _ hsrc (.obj ho hm)
  | cell hc hm => exact access.closed _ _ hsrc (.cell hc hm)

private theorem CheckedLink.unique {p : Program} {w : World} (a b : CheckedLink p w) : a = b := by
  obtain ⟨units, state, a⟩ := a
  obtain ⟨units', state', b⟩ := b
  have equal := a.symm.trans b
  injection equal with hUnits hState
  subst units' state'
  rfl

/-- Validate the finite witness once, then use it in all worlds. -/
def checkPivotAll (ordinal : Nat) (source candidate : Program) :
    Except String (PLift (NormalEquivalent source candidate ∧ SameExports source candidate)) := do
  let before ← (checkLink source).mapError ("original linker: " ++ ·)
  let after ← (checkLink candidate).mapError ("candidate linker: " ++ ·)
  let some ⟨initial⟩ := StateMatch.probe before.ready after.ready |
    throw "the linked initial states differ"
  let check ← checkPivot ordinal before after initial
  let ⟨exportsEqual⟩ ←
    if checked : exportsMatchFrom before after = true then pure (PLift.up checked)
    else throw "the linked entry exports differ"
  have sameExports : SameExports source candidate := sameExports_of_match fun w => by
    rw [exportsMatch_from_checked (before.forWorld w) (after.forWorld w)]
    exact exportsEqual
  return ⟨⟨normalEquivalent_of_checkedPivot (fun w =>
    have hs := check.layout.sourceLink.unique before
    have hc := check.layout.candidateLink.unique after
    let layout : PivotLayout source candidate w := {
      sourceLink := before.forWorld w
      candidateLink := after.forWorld w
      initial := congrArg (linkState w) initial
      pre := check.layout.pre
      crossed := check.layout.crossed
      pivot := check.layout.pivot
      suffix := check.layout.suffix
      sourceOrder := by
        change SameUnits (unitsOf before.linked) _
        exact hs ▸ check.layout.sourceOrder
      candidateOrder := by
        change SameUnits (unitsOf after.linked) _
        exact hc ▸ check.layout.candidateOrder
    }
    {
      layout
      access := Access.linkState (hs ▸ check.access)
      localFlag := check.localFlag
      scope := check.scope
    }), sameExports⟩⟩

end CoreJS.LinkedBehavior
