import EsmSplit.CoreJS.CheckedPivotSound
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.StateMatch

namespace CoreJS.LinkedBehavior

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
