import EsmSplit.CoreJS.HiddenRegion
import EsmSplit.CoreJS.PrefixProjection
import EsmSplit.CoreJS.LinkedPivotCheck
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.StateMatch
import EsmSplit.CoreJS.StructuralRefusal

namespace CoreJS.LinkedBehavior

theorem checkedPivot_move {W : World} {ready : State W} {pre crossed suffix} {pivot : LinkedUnit}
    (n : Nat) (final : State W) (localStep : PivotLocal W n pivot) (checks : CheckedPivot ready pre crossed pivot) :
    runUnits n (pre ++ crossed ++ [pivot] ++ suffix) ready = .ok () final ↔
      runUnits n (pre ++ [pivot] ++ crossed ++ suffix) ready = .ok () final := by
  refine runUnits_pivot_normal n final (fun _ => False) _ checks.access
    localStep ?_
  intro u hu
  obtain ⟨away, scope⟩ := checks.scope u hu
  refine ⟨fun eq => away (congrArg (· + 1) eq), [], by simp, ?_⟩
  rw [← capture_faithful n u.env u.stmt u.stmt.free (fun _ h => h)]
  have frames := evaluatorFrames W (r := pivot.id + 1) (by omega)
    (R := fun _ => False) (fun _ _ h => h) n
  have step := frames.stmt [] (u.env.capture u.stmt.free) u.stmt scope
  refine ⟨step.replace, step.preserves, ?_⟩
  intro s access away
  have result := step.accessible s access away
  cases h : execStmt n (u.env.capture u.stmt.free) u.stmt s <;> rw [h] at result
  case ok v t => exact ⟨trivial, result.2⟩
  all_goals exact result

/-- A successful frame check and a local pivot preserve successful final states across the move. -/
theorem pivotFrameOk_move {W : World} {ready : State W} {pre crossed : List PreparedUnit}
    {suffix : List LinkedUnit} {pivot : LinkedUnit} (n : Nat) (final : State W)
    (localStep : PivotLocal W n pivot) (ok : pivotFrameOk ready pre crossed pivot = true) :
    runUnits n (pre.map (·.unit) ++ crossed.map (·.unit) ++ [pivot] ++ suffix) ready = .ok () final ↔
      runUnits n (pre.map (·.unit) ++ [pivot] ++ crossed.map (·.unit) ++ suffix) ready = .ok () final :=
  checkedPivot_move n final localStep (pivotFrameOk_sound ok)

#print axioms checkedPivot_move
#print axioms pivotFrameOk_move

private theorem Access.linkState {w cells A} {s : State World.probe} (access : Access cells A s) :
    Access cells A (linkState w s) := by
  refine ⟨access.handed, access.supplied, ?_⟩
  intro src dst hsrc edge
  cases edge with
  | obj ho hm => exact access.closed _ _ hsrc (.obj ho hm)
  | cell hc hm => exact access.closed _ _ hsrc (.cell hc hm)

private theorem CheckedPivot.forWorld {ready pre crossed pivot}
    (checks : CheckedPivot ready pre crossed pivot) (w : World) :
    CheckedPivot (linkState w ready) pre crossed pivot :=
  ⟨Access.linkState checks.access, checks.scope⟩

private abbrev original (units : List PreparedUnit) := units.map PreparedUnit.unit

private structure CheckedSchedule (intrinsics : List StableIntrinsic) (ready : State World.probe) (source : List PreparedUnit) where
  units : List LinkedUnit
  moves : Nat
  equivalent : ∀ w, StableIntrinsic.Worlds intrinsics w → ∀ n (final : State w),
    runUnits n (source.map (·.unit)) (linkState w ready) = .ok () final ↔
      runUnits n units (linkState w ready) = .ok () final

private abbrev MoveEquivalent (intrinsics : List StableIntrinsic) (ready : State World.probe) (pre crossed : List PreparedUnit) (pivot : PreparedUnit) :=
  ∀ (suffix : List LinkedUnit) w, StableIntrinsic.Worlds intrinsics w → ∀ n (final : State w),
    runUnits n (original pre ++ original crossed ++ [pivot.unit] ++ suffix) (linkState w ready) = .ok () final ↔
      runUnits n (original pre ++ [pivot.unit] ++ original crossed ++ suffix) (linkState w ready) = .ok () final

/-- Peel observer rows in order, proving isolation in the current heap before
recursing. Restore each entire row before the arbitrary suffix runs. -/
private def checkMaskedMove? (intrinsics : List StableIntrinsic) (ready : State World.probe) (pre crossed : List PreparedUnit) (pivot : PreparedUnit)
    (localStep : PivotLocalFor intrinsics pivot.unit) :
    List Nat → Option (PLift (MoveEquivalent intrinsics ready pre crossed pivot))
  | [] => do
    let ⟨checks⟩ ← (checkPivot ready pre crossed pivot.unit).toOption
    return ⟨fun _ w hw n final => checkedPivot_move n final (localStep w hw n) (checks.forWorld w)⟩
  | q :: rest => do
    let masked := ready.put q #[] #[]
    let ⟨hq, access, scope⟩ ← checkIsolated? masked q (pre ++ crossed ++ [pivot])
    let ⟨inner⟩ ← checkMaskedMove? intrinsics masked pre crossed pivot localStep rest
    return ⟨by
      intro suffix w hw n final
      simp only [List.map_append, List.map_cons, List.map_nil] at scope
      let H := ready.heap[q]?.getD #[]
      let C := ready.cells[q]?.getD #[]
      have restore : (linkState w masked).put q H C = linkState w ready := by
        change linkState w ((ready.put q #[] #[]).put q H C) = _
        rw [State.put_put]
        simp only [State.put, H, C, set!_same rfl]
      have reordered : ∀ u ∈ original pre ++ [pivot.unit] ++ original crossed, IsolatedUnit q u := by
        intro u hu
        apply scope
        simpa only [List.mem_append, List.mem_singleton, or_assoc, or_left_comm, or_comm] using hu
      rw [← restore, runUnits_restore hq n _ suffix scope _ _ (Access.linkState access) H C,
        runUnits_restore hq n _ suffix reordered _ _ (Access.linkState access) H C]
      apply exists_congr
      intro t
      apply and_congr_left
      intro _
      simpa only [List.append_nil] using inner [] w hw n t⟩

/-- Instantiation has already initialized hoisted functions in both links. For an
inert body step, even a crossed unit that calls the function commutes with it.
An allocating initializer still needs the region and capture checks. -/
private def checkMove (intrinsics : List StableIntrinsic) (ready : State World.probe) (pre crossed suffix : List PreparedUnit) (pivot : PreparedUnit) :
    Except StructuralRefusal (PLift (∀ w, StableIntrinsic.Worlds intrinsics w → ∀ n (final : State w),
      runUnits n ((pre ++ crossed ++ [pivot] ++ suffix).map (·.unit)) (linkState w ready) = .ok () final ↔
        runUnits n ((pre ++ [pivot] ++ crossed ++ suffix).map (·.unit)) (linkState w ready) = .ok () final)) := do
  if inert : pivot.unit.stmt.inert = true then
    return ⟨by
      intro w hw n final
      simp only [List.map_append, List.map_cons, List.map_nil]
      apply Iff.symm
      apply runUnits_pivot n _ _ _ pivot.unit (fun _ => True) _ _
        (fun _ _ => trivial) (fun _ _ _ _ _ _ _ => trivial)
      intro u _
      exact ⟨fun s _ => inert_commute inert s, fun s _ => commute_inert inert s⟩⟩
  let some ⟨localStep⟩ := checkedLocal? intrinsics pivot.unit | throw (.coded .nonLocalAllocation
    s!"unit {pivot.unit.id} is not a local allocating declaration")
  match checkPivot ready pre crossed pivot.unit with
  | .ok ⟨checks⟩ => return ⟨by
      intro w hw n final
      simpa only [List.map_append, List.map_cons, List.map_nil] using
        checkedPivot_move (suffix := original suffix) n final (localStep w hw n) (checks.forWorld w)⟩
  | .error why =>
    let qs := observerRegions ready (pivot.unit.id + 1)
    if qs.isEmpty then throw why
    let masked := checkMaskedMove? intrinsics ready pre crossed pivot localStep
    let some ⟨move⟩ := masked qs <|> (do
      let outer ← observerClosure? ready (pivot.unit.id + 1)
      guard (outer != qs)
      masked outer) | throw why
    return ⟨by simpa only [original, List.map_append, List.map_cons, List.map_nil] using move (original suffix)⟩

/-- Move an ordered run across the same crossed code one declaration at a time.
Each check sees the prefix produced by the previous one. -/
private def checkRun? (intrinsics : List StableIntrinsic) (ready : State World.probe)
    (pre crossed suffix : List PreparedUnit) : (group : List PreparedUnit) →
    Option (PLift (∀ w, StableIntrinsic.Worlds intrinsics w → ∀ n (final : State w),
      runUnits n (original (pre ++ crossed ++ group ++ suffix)) (linkState w ready) = .ok () final ↔
        runUnits n (original (pre ++ group ++ crossed ++ suffix)) (linkState w ready) = .ok () final))
  | [] => some ⟨by simp⟩
  | a :: rest => do
    let ⟨first⟩ ← (checkMove intrinsics ready pre crossed (rest ++ suffix) a).toOption
    let ⟨tail⟩ ← checkRun? intrinsics ready (pre ++ [a]) crossed suffix rest
    return ⟨by
      intro w hw n final
      have head := first w hw n final
      have remaining := tail w hw n final
      simp only [original, List.append_assoc, List.cons_append, List.nil_append] at head remaining ⊢
      exact head.trans remaining⟩

/-- Rotate a local initializer across other local steps in distinct regions;
their stored captures may mention each other. -/
private theorem rotateLocal (n : Nat) (s final : State w) (pre group suffix : List LinkedUnit) (b : LinkedUnit)
    (hb : PivotLocal w n b) (locals : ∀ a ∈ group, b.id ≠ a.id ∧ PivotLocal w n a) :
    runUnits n (pre ++ [b] ++ group ++ suffix) s = .ok () final ↔
      runUnits n (pre ++ group ++ [b] ++ suffix) s = .ok () final := by
  apply runUnits_pivot n pre group suffix b (fun _ => True) _ _ (fun _ _ => trivial) (fun _ _ _ _ _ _ _ => trivial)
  intro a ha
  obtain ⟨diff, localA⟩ := locals a ha
  exact ⟨fun t _ => local_units_commute diff hb localA nofun nofun t,
    fun t _ => local_units_commute diff.symm localA hb nofun nofun t⟩

/-- For crossed code X, a local group G and its later initializer b, prove
X G b ≃ X b G ≃ b X G ≃ b G X ≃ G b X. Only the two rotations use
local commutation; every crossing of X uses the regular linked move check. -/
private def checkGroup? (intrinsics : List StableIntrinsic) (ready : State World.probe)
    (pre crossed group suffix : List PreparedUnit) (b : PreparedUnit) :
    Option (PLift (∀ w, StableIntrinsic.Worlds intrinsics w → ∀ n (final : State w),
      runUnits n (original (pre ++ crossed ++ group ++ [b] ++ suffix)) (linkState w ready) = .ok () final ↔
        runUnits n (original (pre ++ group ++ [b] ++ crossed ++ suffix)) (linkState w ready) = .ok () final)) := do
  let ⟨hb⟩ ← checkedLocal? intrinsics b.unit
  if checked : group.all (fun a => b.unit.id != a.unit.id && (checkedLocal? intrinsics a.unit).isSome) then
    let ⟨moveB⟩ ← (checkMove intrinsics ready pre crossed (group ++ suffix) b).toOption
    let ⟨moveGroup⟩ ← checkRun? intrinsics ready (pre ++ [b]) crossed suffix group
    return ⟨by
      intro w hw n final
      have locals : ∀ a ∈ original group, b.unit.id ≠ a.id ∧ PivotLocal w n a := by
        intro a ha
        obtain ⟨u, hu, rfl⟩ := List.mem_map.mp ha
        have ok := List.all_eq_true.mp checked u hu
        simp only [Bool.and_eq_true] at ok
        exact ⟨bne_iff_ne.mp ok.1, ((checkedLocal? intrinsics u.unit).get ok.2).down w hw n⟩
      have before := rotateLocal n (linkState w ready) final (original pre ++ original crossed)
        (original group) (original suffix) b.unit (hb w hw n) locals
      have after := rotateLocal n (linkState w ready) final (original pre)
        (original group) (original crossed ++ original suffix) b.unit (hb w hw n) locals
      have first := moveB w hw n final
      have next := moveGroup w hw n final
      simp only [original, List.map_append, List.map_cons, List.append_assoc,
        List.cons_append, List.nil_append] at before first next after ⊢
      exact before.symm.trans (first.trans (next.trans after))⟩
  else none

/-- Length of the intervening run before the first forward capture, provided
the source and requested schedules agree through that initializer. -/
private def forwardGap? (regions : List Nat) : List PreparedUnit → List Nat → Option Nat
  | u :: us, id :: ids => do
    guard (u.unit.id == id)
    if regions.contains (id + 1) then return 0
    return (← forwardGap? regions us ids) + 1
  | _, _ => none

/-- Match the requested order from left to right. Each move is checked against
the prefix already placed, including units moved by previous steps. -/
private def checkSchedule (intrinsics : List StableIntrinsic) (ready : State World.probe) (prefixRev remaining : List PreparedUnit) :
    List Nat → Except StructuralRefusal (CheckedSchedule intrinsics ready (prefixRev.reverse ++ remaining))
  | [] => do
    unless remaining.isEmpty do throw (.unclassified "the candidate schedule omits linked units")
    return ⟨original (prefixRev.reverse ++ remaining), 0, fun _ _ _ _ => Iff.rfl⟩
  | id :: rest => do
    match h : remaining with
    | u :: tail =>
      if u.unit.id == id then
        let next ← checkSchedule intrinsics ready (u :: prefixRev) tail rest
        return ⟨next.units, next.moves, by
          simpa only [h, List.reverse_cons, List.append_assoc, List.singleton_append] using next.equivalent⟩
    | [] => pure ()
    let crossed := remaining.takeWhile (fun u => u.unit.id != id)
    match h : remaining.dropWhile (fun u => u.unit.id != id) with
    | [] => throw (.unclassified s!"unit {id} is missing from the remaining linked schedule")
    | pivot :: suffix =>
      have split : remaining = crossed ++ pivot :: suffix := by
        have joined := List.takeWhile_append_dropWhile (p := fun u : PreparedUnit => u.unit.id != id) (l := remaining)
        rw [h] at joined
        exact joined.symm
      let regions := (pivot.nodes.get.map (·.addr.region)).filter (· != pivot.unit.id + 1)
      if !regions.isEmpty then
        if let some k := forwardGap? regions suffix rest then
          let middle := suffix.take k
          let group := pivot :: middle
          match hs : suffix.drop k with
          | b :: tail =>
            if let some ⟨move⟩ := checkGroup? intrinsics ready prefixRev.reverse crossed group tail b then
              let next ← checkSchedule intrinsics ready (b :: group.reverse ++ prefixRev) (crossed ++ tail) (rest.drop (k + 1))
              return ⟨next.units, next.moves + group.length + 1, by
                intro w hw n final
                have parted : suffix = middle ++ b :: tail := by
                  rw [← hs]; exact (List.take_append_drop k suffix).symm
                have before := move w hw n final
                have after := next.equivalent w hw n final
                simp only [split, parted, group, List.reverse_cons, List.reverse_append, List.reverse_reverse,
                  List.append_assoc, List.cons_append, List.nil_append, original] at before after ⊢
                exact before.trans after⟩
          | [] => pure ()
      let ⟨move⟩ ← checkMove intrinsics ready prefixRev.reverse crossed suffix pivot
      let next ← checkSchedule intrinsics ready (pivot :: prefixRev) (crossed ++ suffix) rest
      return ⟨next.units, next.moves + 1, by
        intro w hw n final
        rw [split]
        have step := move w hw n final
        have tail := next.equivalent w hw n final
        simp only [List.reverse_cons, List.append_assoc, List.singleton_append] at step tail ⊢
        exact step.trans tail⟩

termination_by ids => ids.length
decreasing_by all_goals simp_all <;> omega

inductive StructuralProof where
  | preservedOrder
  | singlePivot
  | multiplePivots

/-- The schedule checker also serves proof-local sequences whose executions
have already been related to the original unit lists. -/
private def checkRuns (intrinsics : List StableIntrinsic) (ready : State World.probe)
    (source candidate : List LinkedUnit) :
    Except StructuralRefusal (Nat × PLift (∀ w, StableIntrinsic.Worlds intrinsics w → ∀ n (final : State w),
      runUnits n source (linkState w ready) = .ok () final ↔ runUnits n candidate (linkState w ready) = .ok () final)) := do
  let schedule ← checkSchedule intrinsics ready [] (source.map PreparedUnit.ofUnit) (candidate.map LinkedUnit.id)
  let some ⟨same⟩ := sameUnits? schedule.units candidate |
    throw (.unclassified "the linked schedules differ in unit order, syntax, or scope")
  return (schedule.moves, ⟨by
    intro w hw n final
    rw [← runUnits_same n same]
    simpa [List.map_map, Function.comp_def, PreparedUnit.ofUnit] using schedule.equivalent w hw n final⟩)

abbrev ScheduleResult (intrinsics : List StableIntrinsic) (source candidate : Program) :=
  (level : Guarantee) × StructuralProof ×
    PLift (ScheduleEquivalent level (StableIntrinsic.Worlds intrinsics) source candidate ∧ SameExports source candidate)

/-- Check the complete linked schedules. Callers checking additional public
interfaces can use the same linker witnesses. -/
def checkLinkedSchedules (intrinsics : List StableIntrinsic) {source candidate : Program}
    (before : CheckedLink source World.probe) (after : CheckedLink candidate World.probe)
    (recordPivots : Std.HashSet Nat) :
    Except StructuralRefusal (ScheduleResult intrinsics source candidate) := do
  let some ⟨initial⟩ := StateMatch.probe before.ready after.ready |
    throw (.unclassified "the linked initial states differ")
  let ⟨exportsEqual⟩ ←
    if checked : exportsMatchFrom before after = true then pure (PLift.up checked)
    else do
      let a ← (EntryExports.resolve source.entry before.linked).mapError fun why =>
        .unclassified ("original entry: " ++ why)
      let b ← (EntryExports.resolve candidate.entry after.linked).mapError fun why =>
        .unclassified ("candidate entry: " ++ why)
      throw (.unclassified (EntryExports.mismatch a b))
  have sameExports : SameExports source candidate := sameExports_of_match fun w => by
    rw [exportsMatch_from_checked (before.forWorld w) (after.forWorld w)]
    exact exportsEqual
  let sourceUnits := unitsOf before.linked
  let candidateUnits := unitsOf after.linked
  if let some ⟨same⟩ := sameUnits? sourceUnits candidateUnits then
    have equivalent : ScheduleEquivalent .allOutcomes (StableIntrinsic.Worlds intrinsics) source candidate := by
      intro w _ n
      rw [run_after_link n (before.forWorld w), run_after_link n (after.forWorld w)]
      change runUnits n sourceUnits (linkState w before.ready) =
        runUnits n candidateUnits (linkState w after.ready)
      rw [← congrArg (linkState w) initial, runUnits_same n same]
    return ⟨.allOutcomes, .preservedOrder, ⟨equivalent, sameExports⟩⟩
  let runs ← match checkRuns intrinsics before.ready sourceUnits candidateUnits with
    | .ok ok => pure ok
    | .error why => do
      unless sourceUnits.any (fun u => match u.stmt with
        | .decl _ _ (.get (.var _) (.str _)) => true
        | .decl _ _ (.obj fields) => fields.any (fun field =>
            (field matches .init (.var _) _) || (field matches .init _ (.var _)))
        | .decl _ _ (.binary op _ _) => op.numberArithmetic
        | _ => false) do throw why
      let ⟨beforeUnits, ⟨left⟩⟩ := PrefixProjection.prepare recordPivots before.ready sourceUnits
      let ⟨afterUnits, ⟨right⟩⟩ := PrefixProjection.prepare recordPivots before.ready candidateUnits
      let (count, ⟨middle⟩) ← checkRuns intrinsics before.ready beforeUnits afterUnits
      pure (count, PLift.up (fun w hw n final =>
        (left w n final).trans ((middle w hw n final).trans (right w n final).symm)))
  have equivalent : NormalEquivalent (StableIntrinsic.Worlds intrinsics) source candidate := normalEquivalent_of_bounded fun w hw n final => by
    rw [run_after_link n (before.forWorld w), run_after_link n (after.forWorld w)]
    change runUnits n sourceUnits (linkState w before.ready) = .ok () final ↔
      runUnits n candidateUnits (linkState w after.ready) = .ok () final
    rw [← congrArg (linkState w) initial]
    exact runs.2.down w hw n final
  let method := match runs.1 with
    | 0 => .preservedOrder
    | 1 => .singlePivot
    | _ => .multiplePivots
  return ⟨.normalCompletion, method, ⟨equivalent, sameExports⟩⟩

def checkStructuralSchedule (intrinsics : List StableIntrinsic) (source candidate : Program)
    (recordPivots : Std.HashSet Nat) :
    Except StructuralRefusal (ScheduleResult intrinsics source candidate) := do
  let before ← (checkLink source).mapError fun why => .unclassified ("original linker: " ++ why)
  let after ← (checkLink candidate).mapError fun why => .unclassified ("candidate linker: " ++ why)
  checkLinkedSchedules intrinsics before after recordPivots

end CoreJS.LinkedBehavior
