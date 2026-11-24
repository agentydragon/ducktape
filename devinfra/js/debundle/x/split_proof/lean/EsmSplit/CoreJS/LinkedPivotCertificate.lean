import EsmSplit.CoreJS.EvaluatorFrame
import EsmSplit.CoreJS.LinkedPivotCheck
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.StateMatch

namespace CoreJS.LinkedBehavior

private theorem runUnits_move {W : World} {ready : State W} {pre crossed suffix pivot}
    (n : Nat) (final : State W) (checks : CheckedPivot ready pre crossed pivot) :
    runUnits n (pre ++ crossed ++ [pivot] ++ suffix) ready = .ok () final ↔
      runUnits n (pre ++ [pivot] ++ crossed ++ suffix) ready = .ok () final := by
  refine runUnits_pivot_normal n final (fun _ => False) _ checks.access
    (localPivotOk_sound checks.localFlag n) ?_
  intro u hu
  obtain ⟨away, scope⟩ := checks.scope u hu
  refine ⟨fun eq => away (congrArg (· + 1) eq), [], by simp, ?_⟩
  rw [← capture_faithful n u.2.2 u.2.1 u.2.1.free (fun _ h => h)]
  have frames := evaluatorFrames W (r := pivot.1 + 1) (by omega)
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

private abbrev LinkedUnit := Nat × Stmt × Env

private structure CheckedSchedule (ready : State World.probe) (source : List LinkedUnit) where
  units : List LinkedUnit
  moves : Nat
  equivalent : ∀ w n (final : State w),
    runUnits n source (linkState w ready) = .ok () final ↔
      runUnits n units (linkState w ready) = .ok () final

/-- Match the requested order from left to right. Each move is checked against
the prefix already placed, including units moved by previous steps. -/
private def checkSchedule (ready : State World.probe) (prefixRev remaining : List LinkedUnit) :
    List Nat → Except String (CheckedSchedule ready (prefixRev.reverse ++ remaining))
  | [] => do
    unless remaining.isEmpty do throw "the candidate schedule omits linked units"
    return ⟨prefixRev.reverse ++ remaining, 0, fun _ _ _ => Iff.rfl⟩
  | id :: rest => do
    match h : remaining with
    | u :: tail =>
      if u.1 == id then
        let next ← checkSchedule ready (u :: prefixRev) tail rest
        return ⟨next.units, next.moves, by
          simpa only [h, List.reverse_cons, List.append_assoc, List.singleton_append] using next.equivalent⟩
    | [] => pure ()
    let crossed := remaining.takeWhile (fun u => u.1 != id)
    match h : remaining.dropWhile (fun u => u.1 != id) with
    | [] => throw s!"unit {id} is missing from the remaining linked schedule"
    | pivot :: suffix =>
      let ⟨checks⟩ ← checkPivot ready prefixRev.reverse crossed pivot
      let next ← checkSchedule ready (pivot :: prefixRev) (crossed ++ suffix) rest
      return ⟨next.units, next.moves + 1, by
        intro w n final
        have split : remaining = crossed ++ pivot :: suffix := by
          have joined := List.takeWhile_append_dropWhile (p := fun u : LinkedUnit => u.1 != id)
            (l := remaining)
          rw [h] at joined
          exact joined.symm
        rw [split]
        have step := runUnits_move (suffix := suffix) n final
          (⟨Access.linkState checks.access, checks.localFlag, checks.scope⟩ :
            CheckedPivot (linkState w ready) prefixRev.reverse crossed pivot)
        have tail := next.equivalent w n final
        simp only [List.reverse_cons, List.append_assoc, List.singleton_append] at step tail ⊢
        exact step.trans tail⟩

inductive StructuralProof where
  | preservedOrder
  | singlePivot
  | multiplePivots

/-- Select the rule from the linked schedules, then validate the witness in all worlds. -/
def checkStructuralSchedule (source candidate : Program) :
    Except String (StructuralProof × PLift (NormalEquivalent source candidate ∧ SameExports source candidate)) := do
  let before ← (checkLink source).mapError ("original linker: " ++ ·)
  let after ← (checkLink candidate).mapError ("candidate linker: " ++ ·)
  let some ⟨initial⟩ := StateMatch.probe before.ready after.ready |
    throw "the linked initial states differ"
  let ⟨exportsEqual⟩ ←
    if checked : exportsMatchFrom before after = true then pure (PLift.up checked)
    else throw "the linked entry exports differ"
  have sameExports : SameExports source candidate := sameExports_of_match fun w => by
    rw [exportsMatch_from_checked (before.forWorld w) (after.forWorld w)]
    exact exportsEqual
  let sourceUnits := unitsOf before.linked
  let candidateUnits := unitsOf after.linked
  let schedule ← checkSchedule before.ready [] sourceUnits (candidateUnits.map (·.1))
  let some ⟨same⟩ := sameUnits? schedule.units candidateUnits |
    throw "the linked schedules differ in unit order, syntax, or scope"
  have equivalent : NormalEquivalent source candidate := normalEquivalent_of_bounded fun w n final => by
    rw [run_after_link n (before.forWorld w), run_after_link n (after.forWorld w)]
    change runUnits n sourceUnits (linkState w before.ready) = .ok () final ↔
      runUnits n candidateUnits (linkState w after.ready) = .ok () final
    rw [← runUnits_same n same, ← congrArg (linkState w) initial]
    exact schedule.equivalent w n final
  let method := match schedule.moves with
    | 0 => .preservedOrder
    | 1 => .singlePivot
    | _ => .multiplePivots
  return (method, ⟨⟨equivalent, sameExports⟩⟩)

end CoreJS.LinkedBehavior
